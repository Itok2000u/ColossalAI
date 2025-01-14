import pytest
import colossalai
import torch
import torch.multiprocessing as mp
from colossalai.testing import rerun_if_address_is_in_use
from colossalai.utils.cuda import get_current_device
from colossalai.utils import free_port
from colossalai.utils.model.colo_init_context import ColoInitContext
from colossalai.gemini import ChunkManager
from functools import partial
from _utils import tensor_equal, set_seed, tensor_shard_equal
from tests.components_to_test.registry import non_distributed_component_funcs
from torch.nn.parallel import DistributedDataParallel as DDP
from colossalai.nn.parallel import ZeroDDP
from colossalai.nn.optimizer import HybridAdam
from colossalai.zero import ZeroOptimizer
from colossalai.testing import parameterize
from colossalai.amp import convert_to_apex_amp
from colossalai.gemini.gemini_mgr import GeminiManager
from colossalai.tensor import ColoTensorSpec, ShardSpec, ComputePattern, ComputeSpec, DistSpecManager, ProcessGroup


def check_param_equal(model, torch_model, pg: ProcessGroup):
    for p, torch_p in zip(model.parameters(), torch_model.parameters()):
        if p.storage().size() > 0:
            assert p.dtype == torch.half
            assert tensor_shard_equal(torch_p.to(dtype=p.dtype, device=p.device), p, pg.tp_local_rank(),
                                      pg.tp_world_size()), f'{torch_p} vs {p}'


def check_grad_equal(model, torch_model, pg: ProcessGroup):
    for p, torch_p in zip(model.parameters(), torch_model.parameters()):
        if p.grad is not None:
            assert tensor_shard_equal(torch_p.grad.to(dtype=p.grad.dtype, device=p.grad.device), p.grad,
                                      pg.tp_local_rank(), pg.tp_world_size())


def run_fwd_bwd(model, criterion, optimizer, input_ids, attn_mask):
    optimizer.zero_grad()
    logits = model(input_ids, attn_mask)
    logits = logits.float()
    loss = criterion(logits, input_ids)
    optimizer.backward(loss)
    return logits


def init_1d_row_spec(model, pg: ProcessGroup):
    spec = (ShardSpec([0], [pg.tp_world_size()]), ComputeSpec(ComputePattern.TP1D))
    with DistSpecManager.no_grad():
        for n, p in model.named_parameters():
            if 'weight' in n and 'ln' not in n:
                p.set_tensor_spec(*spec)


def init_1d_col_spec(model, pg: ProcessGroup):
    spec = (ShardSpec([-1], [pg.tp_world_size()]), ComputeSpec(ComputePattern.TP1D))
    with DistSpecManager.no_grad():
        for n, p in model.named_parameters():
            if 'ln' not in n and ('weight' in n or 'bias' in n):
                p.set_tensor_spec(*spec)


@parameterize('use_chunk', [False, True])
@parameterize('use_zero', [False, True])
@parameterize('placement_policy', ['cuda', 'cpu'])
def run_gpt(use_chunk, use_zero, placement_policy, tp_init_spec_func=None):
    set_seed(42)
    get_components_func = non_distributed_component_funcs.get_callable('gpt2')
    model_builder, train_dataloader, test_dataloader, optimizer_class, criterion = get_components_func()

    with ColoInitContext(device=get_current_device()):
        model = model_builder()
    model = model.cuda().half()
    torch_model = model_builder().cuda()
    for torch_p, p in zip(torch_model.parameters(), model.parameters()):
        torch_p.data.copy_(p)

    world_size = torch.distributed.get_world_size()

    # world size, dp = 2, tp =2, construct a hybrid parallelism.
    if world_size == 4:
        pg = ProcessGroup(tp_degree=2)
    else:
        pg = ProcessGroup(tp_degree=world_size)

    if tp_init_spec_func:
        tp_init_spec_func(model, pg)

    chunk_size = ChunkManager.search_chunk_size(model, 8192, 8) if use_chunk else None
    chunk_manager = ChunkManager(chunk_size,
                                 enable_distributed_storage=use_zero,
                                 init_device=GeminiManager.get_default_device(placement_policy))
    gemini_manager = GeminiManager(placement_policy, chunk_manager)
    model = ZeroDDP(model, gemini_manager, pg)
    optim = HybridAdam(model.parameters(), lr=1e-3)
    optim = ZeroOptimizer(optim, model, initial_scale=32)

    amp_config = dict(opt_level='O2', keep_batchnorm_fp32=False, loss_scale=32)
    torch_optim = torch.optim.Adam(torch_model.parameters(), lr=1e-3)
    torch_model, torch_optim = convert_to_apex_amp(torch_model, torch_optim, amp_config)
    torch_model = DDP(torch_model, device_ids=[pg.rank()], process_group=pg.dp_process_group())

    # print(chunk_manager)
    check_param_equal(model, torch_model, pg)
    model.train()
    torch_model.train()
    set_seed(pg.dp_local_rank())
    for i, (input_ids, attn_mask) in enumerate(train_dataloader):
        if i > 2:
            break

        logits = run_fwd_bwd(model, criterion, optim, input_ids, attn_mask)
        torch_logits = run_fwd_bwd(torch_model, criterion, torch_optim, input_ids, attn_mask)
        assert tensor_equal(logits, torch_logits)
        check_grad_equal(model, torch_model, pg)
        optim.step()
        torch_optim.step()
        check_param_equal(model, torch_model, pg)


def run_dist(rank, world_size, port):
    config = {}
    colossalai.launch(config=config, rank=rank, world_size=world_size, host='localhost', port=port, backend='nccl')
    if world_size == 4:
        run_gpt(tp_init_spec_func=init_1d_col_spec)
        run_gpt(tp_init_spec_func=init_1d_row_spec)
    else:
        run_gpt()


@pytest.mark.dist
@pytest.mark.skip("under development")
@pytest.mark.parametrize('world_size', [1, 4])
@rerun_if_address_is_in_use()
def test_gpt(world_size):
    run_func = partial(run_dist, world_size=world_size, port=free_port())
    mp.spawn(run_func, nprocs=world_size)


if __name__ == '__main__':
    test_gpt(4)
