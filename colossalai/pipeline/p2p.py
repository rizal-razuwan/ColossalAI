#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import io
import pickle
import re
from collections import namedtuple
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, List, Optional, Union

import torch
import torch.distributed as dist
from packaging.version import Version
from torch.distributed import ProcessGroup
from torch.distributed import distributed_c10d as c10d

from .stage_manager import PipelineStageManager

_unpickler = pickle.Unpickler


def _cuda_safe_tensor_to_object(tensor: torch.Tensor, tensor_size: torch.Size) -> Any:
    """transform tensor to object with unpickle.
    Info of the device in bytes stream will be modified into current device before unpickling

    Args:
        tensor (:class:`torch.tensor`): tensor to be unpickled
        tensor_size (:class:`torch.Size`): Size of the real info in bytes

    Returns:
        Any: object after unpickled
    """
    buf = tensor.numpy().tobytes()[:tensor_size]
    if b"cuda" in buf:
        buf_array = bytearray(buf)
        device_index = torch.cuda.current_device()
        # There might be more than one output tensors during forward
        for cuda_str in re.finditer(b"cuda", buf_array):
            pos = cuda_str.start()
            buf_array[pos + 5] = 48 + device_index
        buf = bytes(buf_array)

    io_bytes = io.BytesIO(buf)
    byte_pickler = _unpickler(io_bytes)
    unpickle = byte_pickler.load()

    return unpickle


# NOTE: FIXME: NPU DOES NOT support isend nor irecv, so broadcast is kept for future use
def _broadcast_object_list(
    object_list: List[Any], src: int, group: ProcessGroup, device: Optional[Union[torch.device, str, int]] = None
):
    """This is a modified version of the broadcast_object_list in torch.distribution
    The only difference is that object will be move to correct device after unpickled.
    If local_rank = src, then object list will be sent to rank src. Otherwise, object list will
    be updated with data sent from rank src.
    Args:
        object_list (List[Any]): list of object to broadcast
        src (int): source rank to broadcast
        dst (int): dst rank to broadcast
        device (:class:`torch.device`): device to do broadcast. current device in default
    """

    if c10d._rank_not_in_group(group):
        c10d._warn_not_in_group("broadcast_object_list")
        return

    is_nccl_backend = check_for_nccl_backend(group)
    current_device = None

    if device is not None:
        if is_nccl_backend and device.type != "cuda":
            raise ValueError("device type must be cuda for nccl backend")
        current_device = device
    else:
        current_device = torch.device("cpu")
        if is_nccl_backend:
            current_device = torch.device("cuda", torch.cuda.current_device())

    my_rank = dist.get_rank()
    # Serialize object_list elements to tensors on src rank.
    if my_rank == src:
        if Version(torch.__version__) >= Version("1.13.0"):
            tensor_list, size_list = zip(*[c10d._object_to_tensor(obj, device=current_device) for obj in object_list])
        else:
            tensor_list, size_list = zip(*[c10d._object_to_tensor(obj) for obj in object_list])
        object_sizes_tensor = torch.cat(size_list)
    else:
        object_sizes_tensor = torch.empty(len(object_list), dtype=torch.long)

    if is_nccl_backend:
        object_sizes_tensor = object_sizes_tensor.to(current_device)

    # Broadcast object sizes
    c10d.broadcast(object_sizes_tensor, src=src, group=group, async_op=False)

    # Concatenate and broadcast serialized object tensors
    if my_rank == src:
        object_tensor = torch.cat(tensor_list)
    else:
        object_tensor = torch.empty(  # type: ignore[call-overload]
            torch.sum(object_sizes_tensor).item(),  # type: ignore[arg-type]
            dtype=torch.uint8,
        )

    if is_nccl_backend:
        object_tensor = object_tensor.to(current_device)

    c10d.broadcast(object_tensor, src=src, group=group, async_op=False)

    # Deserialize objects using their stored sizes.
    offset = 0

    if my_rank != src:
        for i, obj_size in enumerate(object_sizes_tensor):
            obj_view = object_tensor[offset : offset + obj_size]
            obj_view = obj_view.type(torch.uint8)
            if obj_view.device != torch.device("cpu"):
                obj_view = obj_view.cpu()
            offset += obj_size
            # unpickle
            unpickle_object = _cuda_safe_tensor_to_object(obj_view, obj_size)

            # unconsistence in device
            if (
                isinstance(unpickle_object, torch.Tensor)
                and unpickle_object.device.index != torch.cuda.current_device()
            ):
                unpickle_object = unpickle_object.cuda()

            object_list[i] = unpickle_object


def check_for_nccl_backend(group):
    pg = group or c10d._get_default_group()
    # Gate PG wrapper check on Gloo availability.
    if c10d._GLOO_AVAILABLE:
        # It is not expected for PG to be wrapped many times, but support it just
        # in case
        while isinstance(pg, c10d._ProcessGroupWrapper):
            pg = pg.wrapped_pg

    return c10d.is_nccl_available() and pg.name() == c10d.Backend.NCCL


def check_device(group):
    is_nccl_backend = check_for_nccl_backend(group)
    current_device = None

    current_device = torch.device("cpu")
    if is_nccl_backend:
        current_device = torch.device("cuda", torch.cuda.current_device())
    return current_device, is_nccl_backend


TensorMetadata = namedtuple("TensorMetadata", ["key", "shape", "dtype", "requires_grad"])


class P2PDataType(Enum):
    Serialization = 0
    Tensor = 1
    List = 2
    Dict = 3


@dataclass
class P2PMetadata:
    data_type: P2PDataType
    content: Union[List[TensorMetadata], TensorMetadata, Any]


def filling_ops_queue(obj: Any, comm_op: Callable, comm_rank: int, ops_queue: List, group: ProcessGroup):
    if isinstance(obj, torch.Tensor):
        obj = obj.contiguous()
        op_to_add = dist.P2POp(comm_op, obj, comm_rank, group)
        ops_queue.append(op_to_add)
    else:
        for tensor_to_comm in obj:
            assert isinstance(tensor_to_comm, torch.Tensor)
            filling_ops_queue(tensor_to_comm, comm_op, comm_rank, ops_queue, group)


def create_recv_buffer(p2p_metadata: P2PMetadata, current_device: Any):
    if p2p_metadata.data_type == P2PDataType.Tensor:
        metadata = p2p_metadata.content
        tensor_recv = torch.empty(
            metadata.shape, requires_grad=metadata.requires_grad, device=current_device, dtype=metadata.dtype
        )
        return tensor_recv
    elif p2p_metadata.data_type in (P2PDataType.List, P2PDataType.Dict):
        buffer_recv = []
        for metadata in p2p_metadata.content:
            tensor_recv = torch.empty(
                metadata.shape, requires_grad=metadata.requires_grad, device=current_device, dtype=metadata.dtype
            )
            buffer_recv.append(tensor_recv)
        return buffer_recv
    else:
        raise ValueError(f"Unknown data_type: {p2p_metadata.data_type}")


def create_fast_send_metadata(object: Any) -> P2PMetadata:
    assert _check_if_fast_send_available(object)
    if isinstance(object, torch.Tensor):
        data_type = P2PDataType.Tensor
        content = TensorMetadata(None, object.shape, object.dtype, object.requires_grad)
    elif isinstance(object, list):
        data_type = P2PDataType.List
        content = [TensorMetadata(None, v.shape, v.dtype, v.requires_grad) for v in object]
    elif isinstance(object, dict):
        data_type = P2PDataType.Dict
        content = [TensorMetadata(k, v.shape, v.dtype, v.requires_grad) for k, v in object.items()]
    else:
        raise RuntimeError("Cannot handle object of type {}".format(type(object)))
    return P2PMetadata(data_type, content)


def _batch_send_recv_tensor(
    send_tensor_list: Optional[Union[torch.Tensor, List[torch.Tensor]]],
    recv_tensor_metadata: Optional[P2PMetadata],
    send_dst: Optional[int],
    recv_src: Optional[int],
    send_group: Optional[ProcessGroup],
    recv_group: Optional[ProcessGroup],
    current_device: Any,
) -> Optional[Union[torch.Tensor, List[torch.Tensor]]]:
    buffer_recv = None
    if recv_tensor_metadata is not None and recv_tensor_metadata.data_type != P2PDataType.Serialization:
        buffer_recv = create_recv_buffer(recv_tensor_metadata, current_device)

    ops = []
    if send_dst is not None and send_tensor_list is not None:
        assert send_group is not None
        filling_ops_queue(send_tensor_list, dist.isend, send_dst, ops, send_group)
    if recv_src is not None and buffer_recv is not None:
        assert recv_group is not None
        filling_ops_queue(buffer_recv, dist.irecv, recv_src, ops, recv_group)

    if len(ops) > 0:
        reqs = dist.batch_isend_irecv(ops)
        for req in reqs:
            req.wait()

    # Remove synchronization according to Pytorch's documentation
    # However, the Megatron-LM does synchronization here
    # https://github.com/microsoft/Megatron-DeepSpeed/blob/ef13d099c2a1609225a4ce4c1a1753cc76dd90a1/megatron/p2p_communication.py#L111-L112
    # In case there is potential error, uncomment the following `torch.cuda.synchronize()`
    torch.cuda.synchronize()

    return buffer_recv


def _send_recv_serialization_object(
    object: Any,
    send_dst: Optional[int],
    recv_src: Optional[int],
    send_group: Optional[ProcessGroup],
    recv_group: Optional[ProcessGroup],
    current_device: Any,
    is_nccl_backend: bool,
) -> Optional[P2PMetadata]:
    ops = []

    send_object_tensor = None
    if object is not None and send_dst is not None:
        if Version(torch.__version__) >= Version("1.13.0"):
            send_object_tensor, send_object_size_tensor = c10d._object_to_tensor(object, device=current_device)
        else:
            send_object_tensor, send_object_size_tensor = c10d._object_to_tensor(object)

        if is_nccl_backend:
            send_object_size_tensor = send_object_size_tensor.to(current_device)
            send_object_tensor = send_object_tensor.to(current_device)

        filling_ops_queue(send_object_size_tensor, dist.isend, send_dst, ops, send_group)

    recv_object_size_tensor = None
    if recv_src is not None:
        recv_object_size_tensor = torch.empty(1, dtype=torch.long)
        if is_nccl_backend:
            recv_object_size_tensor = recv_object_size_tensor.to(current_device)
        filling_ops_queue(recv_object_size_tensor, dist.irecv, recv_src, ops, recv_group)

    if len(ops) > 0:
        reqs = dist.batch_isend_irecv(ops)
        for req in reqs:
            req.wait()

    # See the comment in `_batch_send_recv_tensor`
    torch.cuda.synchronize()

    ops = []

    if send_dst is not None and send_object_tensor is not None:
        filling_ops_queue(send_object_tensor, dist.isend, send_dst, ops, send_group)

    recv_object_tensor = None
    if recv_src is not None and recv_object_size_tensor is not None:
        recv_object_tensor = torch.empty(recv_object_size_tensor.item(), dtype=torch.uint8)
        if is_nccl_backend:
            recv_object_tensor = recv_object_tensor.to(current_device)
        filling_ops_queue(recv_object_tensor, dist.irecv, recv_src, ops, recv_group)

    if len(ops) > 0:
        reqs = dist.batch_isend_irecv(ops)
        for req in reqs:
            req.wait()

    # See the comment in `_batch_send_recv_tensor`
    torch.cuda.synchronize()

    if recv_object_tensor is not None and recv_object_size_tensor is not None:
        recv_object_tensor = recv_object_tensor.type(torch.uint8)
        if recv_object_tensor.device != torch.device("cpu"):
            recv_object_tensor = recv_object_tensor.cpu()

        unpickle_object = _cuda_safe_tensor_to_object(recv_object_tensor, recv_object_size_tensor.item())

        if isinstance(unpickle_object, torch.Tensor) and unpickle_object.device.index != torch.cuda.current_device():
            unpickle_object = unpickle_object.cuda()

        return unpickle_object


def _check_if_fast_send_available(object: Any) -> bool:
    if isinstance(object, torch.Tensor):
        return True
    elif isinstance(object, list):
        is_list_of_tensor = all([isinstance(v, torch.Tensor) for v in object])
        return is_list_of_tensor
    elif isinstance(object, dict):
        is_dict_of_tensor = all([isinstance(k, str) and isinstance(v, torch.Tensor) for k, v in object.items()])
        return is_dict_of_tensor
    return False


def _communicate(
    object: Any,
    send_dst: Optional[int],
    recv_src: Optional[int],
    send_group: Optional[ProcessGroup] = None,
    recv_group: Optional[ProcessGroup] = None,
    send_metadata: bool = True,
    metadata_recv: Optional[P2PMetadata] = None,
) -> Any:
    """
    Send and receive object from send_dst and recv_src respectively

    Args:
        object (Any): object needed to be sent
        send_dst (int): rank of the destination
        recv_src (int): rank of the source
        send_group (ProcessGroup, optional): process group of sender
        recv_group (ProcessGroup, optional): process group of receiver
        send_metadata (bool, optional): whether to send metadata
        metadata_recv (P2PMetadata, optional): metadata of the object to be received
    """
    assert send_dst is not None or recv_src is not None, "send_dst and recv_src cannot be both None"
    assert send_dst is None or send_group is not None, "send_group must be specified when send_dst is not None"
    assert recv_src is None or recv_group is not None, "recv_group must be specified when recv_src is not None"
    send_metadata = send_metadata or (object is not None and not _check_if_fast_send_available(object))
    assert (
        metadata_recv is None or metadata_recv.data_type != P2PDataType.Serialization
    ), "metadata_recv type must not be Serialization"

    # NOTE: send & recv should be atomic operations. However, if we need to send metadata or receive metadata,
    #   we are not able to do that (1. send & recv metadata 2. send & recv). So we need to split the send & recv into two parts in this case.
    if (send_dst is not None and recv_src is not None) and (send_metadata or metadata_recv is None):
        _communicate(object, send_dst=send_dst, recv_src=None, send_group=send_group, send_metadata=send_metadata)
        return _communicate(None, send_dst=None, recv_src=recv_src, recv_group=recv_group, metadata_recv=metadata_recv)

    # NOTE: only the following 5 cases are valid:
    #   1. send() [needs extra metadata] and no recv()
    #   2. recv() [needs extra metadata] and no send()
    #   3. neither send() nor recv() need extra metadata
    assert not (send_dst is not None and send_metadata) or recv_src is None
    assert not (recv_src is not None and metadata_recv is None) or send_dst is None
    assert not (send_dst is not None and recv_src is not None) or (not send_metadata and metadata_recv is not None)
    assert not c10d._rank_not_in_group(send_group) and not c10d._rank_not_in_group(recv_group)

    current_send_device, is_send_nccl_backend = check_device(send_group)
    current_recv_device, is_recv_nccl_backend = check_device(recv_group)

    is_nccl_backend = is_send_nccl_backend and is_recv_nccl_backend

    assert current_send_device == current_recv_device
    current_device = current_send_device

    if (send_dst is not None and send_metadata) or (recv_src is not None and metadata_recv is None):
        metadata_send = None
        if send_dst is not None and send_metadata:
            can_fast_send = _check_if_fast_send_available(object) and is_nccl_backend
            if not can_fast_send:
                metadata_send = P2PMetadata(P2PDataType.Serialization, object)
            else:
                metadata_send = create_fast_send_metadata(object)

        # Send and receive metadata
        _metadata_recv = _send_recv_serialization_object(
            object=metadata_send,
            send_dst=send_dst if send_metadata else None,
            recv_src=recv_src if metadata_recv is None else None,
            send_group=send_group if send_metadata else None,
            recv_group=recv_group if metadata_recv is None else None,
            current_device=current_device,
            is_nccl_backend=is_nccl_backend,
        )
        assert metadata_recv is None or _metadata_recv is None
        metadata_recv = _metadata_recv if metadata_recv is None else metadata_recv

    send_tensor_list = None
    if isinstance(object, torch.Tensor):
        send_tensor_list = object
    elif isinstance(object, list):
        send_tensor_list = object
    elif isinstance(object, dict):
        send_tensor_list = list(object.values())

    # Send and receive data
    recv_buffer = _batch_send_recv_tensor(
        send_tensor_list, metadata_recv, send_dst, recv_src, send_group, recv_group, current_device
    )

    if metadata_recv is not None:
        assert isinstance(metadata_recv, P2PMetadata)
        if metadata_recv.data_type == P2PDataType.Serialization:
            return metadata_recv.content
        else:
            assert recv_buffer is not None
            if metadata_recv.data_type in [P2PDataType.Tensor, P2PDataType.List]:
                return recv_buffer
            elif metadata_recv.data_type == P2PDataType.Dict:
                return {k: v for k, v in zip([m.key for m in metadata_recv.content], recv_buffer)}
            else:
                raise ValueError("Unknown data type {}".format(metadata_recv.data_type))


def _send_object(object: Any, src: int, dst: int, group: ProcessGroup, send_metadata: bool) -> None:
    """send anything to dst rank

    Args:
        object (Any): object needed to be sent
        dst (int): rank of the destination

    Returns:
        None
    """
    _communicate(object, send_dst=dst, recv_src=None, send_group=group, send_metadata=send_metadata)


def _recv_object(src: int, dst: int, group: ProcessGroup, metadata_recv: Optional[P2PMetadata]) -> Any:
    """recv anything from src

    Args:
        src (int): source rank of data. local rank will receive data from src rank.

    Returns:
        Any: Object received from src.
    """
    return _communicate(None, send_dst=None, recv_src=src, recv_group=group, metadata_recv=metadata_recv)


def _p2p_comm(
    tensor_send_next: torch.Tensor,
    recv_prev: bool,
    peer: int,
    group: ProcessGroup,
    comm_dtype: torch.dtype = torch.float16,
):
    """
    Send and recv tensor using P2P communication, used when pipeline size is 2 to solve the race communication.

    Args:
        tensor_send_next (torch.Tensor): tensor to be sent to next stage
        recv_prev (bool): whether to receive tensor from previous stage
        peer (int): rank of the peer
        group (ProcessGroup): process group
        comm_dtype (torch.dtype): dtype of the tensor to be sent

    Returns:
        torch.Tensor: tensor received from previous stage
    """
    # send and recv shape
    send_next_shape = None
    recv_prev_shape = None

    if tensor_send_next is not None:
        send_next_shape = torch.tensor(tensor_send_next.size(), device=torch.cuda.current_device(), dtype=torch.int64)
    if recv_prev:
        recv_prev_shape = torch.empty((3), device=torch.cuda.current_device(), dtype=torch.int64)

    ops = []
    if send_next_shape is not None:
        send_next_op = dist.P2POp(dist.isend, send_next_shape, peer=peer, group=group)
        ops.append(send_next_op)
    if recv_prev_shape is not None:
        recv_prev_op = dist.P2POp(
            dist.irecv,
            recv_prev_shape,
            peer=peer,
            group=group,
        )
        ops.append(recv_prev_op)
    if len(ops) > 0:
        reqs = dist.batch_isend_irecv(ops)
        for req in reqs:
            req.wait()

    if recv_prev_shape is not None:
        recv_prev_shape = recv_prev_shape.tolist()

    # send and recv data
    tensor_recv_prev = None
    if recv_prev:
        tensor_recv_prev = torch.empty(recv_prev_shape, device=torch.cuda.current_device(), dtype=comm_dtype)

    ops = []
    if tensor_send_next is not None:
        send_next_op = dist.P2POp(
            dist.isend,
            tensor_send_next,
            peer=peer,
            group=group,
        )
        ops.append(send_next_op)
    if tensor_recv_prev is not None:
        recv_prev_op = dist.P2POp(
            dist.irecv,
            tensor_recv_prev,
            peer=peer,
            group=group,
        )
        ops.append(recv_prev_op)
    if len(ops) > 0:
        reqs = dist.batch_isend_irecv(ops)
        for req in reqs:
            req.wait()
    return tensor_recv_prev


class PipelineP2PCommunication:
    def __init__(self, stage_manager: PipelineStageManager) -> None:
        self.stage_manager = stage_manager

    def recv_forward(self, prev_rank: Optional[int] = None, metadata_recv: Optional[P2PMetadata] = None) -> Any:
        """Copy the forward output from the previous stage in pipeline as the input tensor of this stage.

        Args:
            prev_rank (int, optional): The rank of the source of the tensor.

        Returns:
            Any: The input tensor or input tensor list.
        """
        if prev_rank is None:
            prev_rank = self.stage_manager.get_prev_rank()
        cur_rank = self.stage_manager.get_rank()
        input_tensor = _recv_object(
            prev_rank, cur_rank, self.stage_manager.get_p2p_process_group(prev_rank, cur_rank), metadata_recv
        )

        return input_tensor

    def recv_backward(self, next_rank: Optional[int] = None, metadata_recv: Optional[P2PMetadata] = None) -> Any:
        """Copy the gradient tensor from the next stage in pipeline as the input gradient of this stage.

        Args:
            next_rank (int, optional): The rank of the source of the tensor.

        Returns:
            Any: The input gradient tensor or gradient tensor list.
        """
        if next_rank is None:
            next_rank = self.stage_manager.get_next_rank()
        cur_rank = self.stage_manager.get_rank()
        output_tensor_grad = _recv_object(
            next_rank, cur_rank, self.stage_manager.get_p2p_process_group(next_rank, cur_rank), metadata_recv
        )

        return output_tensor_grad

    def send_forward(self, output_object: Any, next_rank: Optional[int] = None, send_metadata: bool = True) -> None:
        """Sends the input tensor to the next stage in pipeline.

        Args:
            output_object (Any): Object to be sent.
            next_rank (int, optional): The rank of the recipient of the tensor.
        """
        if next_rank is None:
            next_rank = self.stage_manager.get_next_rank()
        cur_rank = self.stage_manager.get_rank()
        _send_object(
            output_object,
            cur_rank,
            next_rank,
            self.stage_manager.get_p2p_process_group(cur_rank, next_rank),
            send_metadata,
        )

    def send_backward(self, input_object: Any, prev_rank: Optional[int] = None, send_metadata: bool = True) -> None:
        """Sends the gradient tensor to the previous stage in pipeline.

        Args:
            input_object (Any): Object to be sent.
            prev_rank (int, optional): The rank of the recipient of the tensor
        """
        if prev_rank is None:
            prev_rank = self.stage_manager.get_prev_rank()
        cur_rank = self.stage_manager.get_rank()
        _send_object(
            input_object,
            cur_rank,
            prev_rank,
            self.stage_manager.get_p2p_process_group(cur_rank, prev_rank),
            send_metadata,
        )

    def send_forward_recv_backward(
        self,
        input_object: Any,
        next_rank: Optional[int] = None,
        send_metadata: bool = True,
        metadata_recv: Optional[P2PMetadata] = None,
    ) -> Any:
        """Sends the gradient tensor to and copy the gradient tensor from the next stage in pipeline

        Args:
            input_object (Any): Object to be sent.
            next_rank (int, optional): The rank of the sender and recipient of the tensor
        """
        if next_rank is None:
            next_rank = self.stage_manager.get_next_rank()

        cur_rank = self.stage_manager.get_rank()
        group = self.stage_manager.get_p2p_process_group(cur_rank, next_rank)
        return _communicate(
            input_object,
            next_rank,
            next_rank,
            send_group=group,
            recv_group=group,
            send_metadata=send_metadata,
            metadata_recv=metadata_recv,
        )

    def send_backward_recv_forward(
        self,
        input_object: Any,
        prev_rank: Optional[int] = None,
        send_metadata: bool = True,
        metadata_recv: Optional[P2PMetadata] = None,
    ) -> Any:
        """Sends the gradient tensor to and copy the gradient tensor from the previous stage in pipeline

        Args:
            input_object (Any): Object to be sent.
            prev_rank (int, optional): The rank of the sender and recipient of the tensor
        """
        if prev_rank is None:
            prev_rank = self.stage_manager.get_prev_rank()

        cur_rank = self.stage_manager.get_rank()
        group = self.stage_manager.get_p2p_process_group(prev_rank, cur_rank)
        return _communicate(
            input_object,
            prev_rank,
            prev_rank,
            send_group=group,
            recv_group=group,
            send_metadata=send_metadata,
            metadata_recv=metadata_recv,
        )

    def p2p_communicate(
        self,
        output_object: Any,
        recv_pre: bool,
        next_rank: Optional[int] = None,
        comm_dtype: torch.dtype = torch.float16,
    ) -> None:
        """
        Sends the input tensor to the next stage in pipeline, using `P2Pop` in torch.

        Args:
            output_object (Any): Object to be sent.
            next_rank (int, optional): The rank of the recipient of the tensor.
        """
        if next_rank is None:
            next_rank = self.stage_manager.get_next_rank()
        cur_rank = self.stage_manager.get_rank()
        recv_tensor = _p2p_comm(
            output_object,
            recv_pre,
            next_rank,
            self.stage_manager.get_p2p_process_group(cur_rank, next_rank),
            comm_dtype,
        )
        return recv_tensor
