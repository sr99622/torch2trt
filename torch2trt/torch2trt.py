import torch
import tensorrt as trt
import copy
import numpy as np
import io
from collections import defaultdict
import importlib

from .calibration import (
    TensorBatchDataset,
    DatasetCalibrator,
    DEFAULT_CALIBRATION_ALGORITHM,
)

# UTILITY FUNCTIONS


def trt_version():
    return trt.__version__


def torch_version():
    return torch.__version__


def torch_dtype_to_trt(dtype):
    if trt_version() >= '7.0' and dtype == torch.bool:
        return trt.bool
    elif dtype == torch.int8:
        return trt.int8
    elif dtype == torch.int32:
        return trt.int32
    elif dtype == torch.float16:
        return trt.float16
    elif dtype == torch.float32:
        return trt.float32
    else:
        raise TypeError("%s is not supported by tensorrt" % dtype)


def torch_dtype_from_trt(dtype):
    if dtype == trt.int8:
        return torch.int8
    elif trt_version() >= '7.0' and dtype == trt.bool:
        return torch.bool
    elif dtype == trt.int32:
        return torch.int32
    elif dtype == trt.float16:
        return torch.float16
    elif dtype == trt.float32:
        return torch.float32
    else:
        raise TypeError("%s is not supported by torch" % dtype)


def torch_device_to_trt(device):
    if device.type == torch.device("cuda").type:
        return trt.TensorLocation.DEVICE
    elif device.type == torch.device("cpu").type:
        return trt.TensorLocation.HOST
    else:
        return TypeError("%s is not supported by tensorrt" % device)


def torch_device_from_trt(device):
    if device == trt.TensorLocation.DEVICE:
        return torch.device("cuda")
    elif device == trt.TensorLocation.HOST:
        return torch.device("cpu")
    else:
        return TypeError("%s is not supported by torch" % device)


def trt_num_inputs(engine):
    count = 0
    for i in range(engine.num_bindings):
        if engine.binding_is_input(i):
            count += 1
    return count


def trt_num_outputs(engine):
    count = 0
    for i in range(engine.num_bindings):
        if not engine.binding_is_input(i):
            count += 1
    return count


def torch_dim_resolve_negative(dim, ndim):
    if not isinstance(dim, tuple):
        dim = (dim,)
    pos = []
    for d in dim:
        if d < 0:
            d = ndim + d
        pos.append(d)
    return tuple(pos)


def torch_dim_to_trt_axes(dim):
    """Converts torch dim, or tuple of dims to a tensorrt axes bitmask"""
    if not isinstance(dim, tuple):
        dim = (dim,)

    # create axes bitmask for reduce layer
    axes = 0
    for d in dim:
        axes |= 1 << d 

    return axes


def add_trt_constant(network, tensor):
    shape = tuple(tensor.shape)
    array = tensor[0].detach().cpu().numpy()
    layer = network.add_constant(shape, array)
    return layer.get_output(0)


def check_torch_dtype(*tensors):
    dtype = None
    for t in tensors:
        if isinstance(t, torch.Tensor):
            if dtype is None:
                dtype = t.dtype
            else:
                assert dtype == t.dtype  # , 'Tensor data types must match')
    assert (
        dtype is not None
    )  # , 'Data type could not be inferred from any item in list')
    return dtype


def add_missing_trt_tensors(network, tensors):
    """Creates missing TensorRT tensors as constants and attaches them to the Torch Tensors"""
    trt_tensors = [None] * len(tensors)

    dtype = check_torch_dtype(*tensors)

    for i, t in enumerate(tensors):
        trt_tensor = None

        # GET TRT TENSOR (OR CREATE TRT CONSTANT)

        # get tensor w/ _trt
        # or... add constant for scalar primitive
        if isinstance(t, float) or isinstance(t, int):
            shape = (1,)
            scalar = t * torch.ones(shape, dtype=dtype).cpu().numpy()
            trt_tensor = network.add_constant(shape, scalar).get_output(0)
        elif hasattr(t, "_trt"):
            trt_tensor = t._trt

        # or... add constant for leaf tensor w/o _trt
        else:

            # remove all preceding ones, these can be re-inserted later when broadcasting
            num_preceding_ones = 0
            for j in range(len(t.shape)):
                if int(t.shape[j]) == 1:
                    num_preceding_ones += 1
                else:
                    break
            shape = tuple(t.shape[num_preceding_ones:])

            weight = t.detach().cpu().numpy()
            t._trt = network.add_constant(shape, weight).get_output(0)
            trt_tensor = t._trt


        assert trt_tensor is not None

        trt_tensors[i] = trt_tensor

    return trt_tensors


def broadcast_trt_tensors(network, trt_tensors, broadcast_ndim):
    """Broadcast TensorRT tensors to the specified dimension by pre-padding shape 1 dims"""
    broadcasted_trt_tensors = [None] * len(trt_tensors)

    for i, t in enumerate(trt_tensors):

        if len(t.shape) < broadcast_ndim:
            # append 1 size dims to front
            diff = broadcast_ndim - len(t.shape)
            shape = tuple([1] * diff + list(t.shape))
            layer = network.add_shuffle(t)
            layer.reshape_dims = shape
            trt_tensor = layer.get_output(0)
        else:
            trt_tensor = t

        broadcasted_trt_tensors[i] = trt_tensor

    return broadcasted_trt_tensors


def trt_(network, *tensors):
    """Creates missing TensorRT tensors and adds shuffle layers to make tensors broadcastable"""
    trt_tensors = [None] * len(tensors)

    dtype = check_torch_dtype(*tensors)

    # get broadcast dimension
    broadcast_num_dim = 0
    for t in tensors:
        if isinstance(t, torch.Tensor):
            if not hasattr(t, "_trt"):
                num_dim = len(t.shape)  # don't exclude batch for constants
            else:
                num_dim = len(
                    t._trt.shape
                )  # non-leaf tensors must already have _trt, get shape from that
            if num_dim > broadcast_num_dim:
                broadcast_num_dim = num_dim

    for i, t in enumerate(tensors):
        trt_tensor = None

        # GET TRT TENSOR (OR CREATE TRT CONSTANT)

        # get tensor w/ _trt
        if isinstance(t, torch.Tensor) and hasattr(t, "_trt"):
            trt_tensor = t._trt

        # or... add constant for leaf tensor w/o _trt
        elif isinstance(t, torch.Tensor) and not hasattr(t, "_trt"):
            # add leaf tensor
            shape = tuple(t.shape)  #  don't exclude batch when adding constants...?
            weight = t.detach().cpu().numpy()
            t._trt = network.add_constant(shape, weight).get_output(0)
            trt_tensor = t._trt

        # or... add constant for scalar primitive
        elif isinstance(t, float) or isinstance(t, int):
            shape = (1,) * broadcast_num_dim
            scalar = t * torch.ones(shape, dtype=dtype).cpu().numpy()
            trt_tensor = network.add_constant(shape, scalar).get_output(0)

        assert trt_tensor is not None

        # MAKE TRT TENSOR BROADCASTABLE IF IT IS NOT ALREADY

        if len(trt_tensor.shape) < broadcast_num_dim:
            # append 1 size dims to front
            diff = broadcast_num_dim - len(trt_tensor.shape)
            shape = tuple([1] * diff + list(trt_tensor.shape))
            layer = network.add_shuffle(trt_tensor)
            layer.reshape_dims = shape
            trt_tensor = layer.get_output(0)

        trt_tensors[i] = trt_tensor

    if len(trt_tensors) == 1:
        return trt_tensors[0]
    else:
        return tuple(trt_tensors)


# CONVERSION REGISTRY AND HOOKS


CONVERTERS = {}


def get_arg(ctx, name, pos, default):
    if name in ctx.method_kwargs:
        return ctx.method_kwargs[name]
    elif len(ctx.method_args) > pos:
        return ctx.method_args[pos]
    else:
        return default


def attach_converter(ctx, method, converter, method_str):
    """Gets a function that executes PyTorch method and TensorRT converter"""
    global DUMMY_CONVERTERS

    def wrapper(*args, **kwargs):
        skip = True

        # check if another (parent) converter has lock
        if not ctx.lock:
            if converter["is_real"]:
                ctx.lock = True  # only real converters can acquire lock
            skip = False

        # run original method
        outputs = method(*args, **kwargs)

        if not skip:
            ctx.method_args = args
            ctx.method_kwargs = kwargs
            ctx.method_return = outputs
            ctx.method_str = method_str

            #             print('%s' % (converter.__name__,))
            converter["converter"](ctx)

            # convert to None so conversion will fail for unsupported layers
            ctx.method_args = None
            ctx.method_kwargs = None
            ctx.method_return = None
            ctx.lock = False

        return outputs

    return wrapper


class ConversionHook(object):
    """Attaches TensorRT converter to PyTorch method call"""

    def __init__(self, ctx, key, converter):
        self.ctx = ctx
        self.key = key
        self.converter = converter

    def _set_method(self, method):
        module = self.converter['module']
        exec('module.%s = method' % self.converter['qual_name'])

    def __enter__(self):
        self._set_method(
            attach_converter(
                self.ctx, self.converter['method_impl'], self.converter, self.converter['method_str']
            )
        )

    def __exit__(self, type, val, tb):
        self._set_method(self.converter['method_impl'])

def default_input_names(num_inputs):
    return ["input_%d" % i for i in range(num_inputs)]

def default_output_names(num_outputs):
    return ["output_%d" % i for i in range(num_outputs)]


def device_type_str(device_type):
    if device_type == trt.DeviceType.GPU:
        return 'GPU'
    elif device_type == trt.DeviceType.DLA:
        return 'DLA'
    

class NetworkWrapper(object):
    def __init__(self, ctx, network):
        self._ctx = ctx
        self._network = network
        self._layer_counts = defaultdict(lambda: 0)

    def _configure_layer(self, layer):
        
        # set layer device type
        device_type = self._ctx.current_device_type()
        self._ctx.builder_config.set_device_type(layer, device_type)
        orig_device_type = device_type
        if not self._ctx.builder_config.can_run_on_DLA(layer) and device_type == trt.DeviceType.DLA:
            if self._ctx.torch2trt_kwargs['gpu_fallback']:
                device_type = trt.DeviceType.GPU  # layer will fall back to GPU
        
        # set layer name
        def arg_str(arg):
            if isinstance(arg, torch.Tensor):
                return "tensor(shape=%s, dtype=%s)" % (str(list(arg.shape)), str(arg.dtype))
            return str(arg)
        scope_name = self._ctx.current_module_name()# + ':' + layer.type.name
        self._layer_counts[scope_name] += 1
        args = [arg_str(arg) for arg in self._ctx.method_args]
        kwargs = ["%s=%s" % (key, arg_str(arg)) for key, arg in self._ctx.method_kwargs.items()]
        layer.name = scope_name + ':' + str(self._layer_counts[scope_name] - 1) + ':' + layer.type.name + ':' + device_type_str(device_type) 
        
        if orig_device_type != device_type:
            layer.name = layer.name + '(' + device_type_str(orig_device_type) + ')'
#         "%s [%s #%d, %s] %s(%s)" % (self._ctx.current_module_name(), layer.type.name, self._layer_counts[layer.type.name], device_type_str(device_type),
#                                           self._ctx.method_str, ", ".join(args + kwargs))
    
        
    def __getattr__(self, name):
        attr = getattr(self._network, name)
        if callable(attr):
            def wrapper(*args, **kwargs):
                ret = attr(*args, **kwargs)
                if isinstance(ret, trt.ILayer):
                    self._configure_layer(ret)
                return ret

            return wrapper
        else:
            return attr



class ConversionContext(object):
    
    def __init__(self, network, converters=CONVERTERS, torch2trt_kwargs=None, builder_config=None):
        self.network = NetworkWrapper(self, network)
        self.lock = False
        self.method_args = None
        self.method_kwargs = None
        self.method_return = None
        self.torch2trt_kwargs = torch2trt_kwargs
        self.builder_config = builder_config
        self.hooks = [
            ConversionHook(self, key, converter)
            for key, converter in converters.items()
        ]
        
        self.module_stack = []
        self.module_handles = []
        self.device_type_stack = []
        self.module_name_map = {}
        for name, module in torch2trt_kwargs['module'].named_modules():
            self.module_name_map[module] = name
        
    def current_module_name(self):
        return self.get_module_name(self.current_module())
    
    def current_module(self):
        return self.module_stack[-1]
    
    def get_module_name(self, module):
        return self.module_name_map[module]
    
    def _module_pre_hook(self, module, input):
        # TODO(@jwelsh): add logging to show module entry / exit
        self.module_stack.append(module)
        
        # hook that is attached to modulee using register_forward_pre_hook, which is called before module is executed
        if module in self.torch2trt_kwargs['device_types']:
            device_type = self.torch2trt_kwargs['device_types'][module]
            self.device_type_stack.append((module, device_type))
        
    def _module_post_hook(self, module, input, output):
        
        # if module was used to set the current device type, pop device type from stack
        if self.current_device_type_module() == module:
            self.device_type_stack.pop()
            
        self.module_stack.pop()
        
    def current_device_type(self):
        """Returns the current device type"""
        if len(self.device_type_stack) > 0:
            return self.device_type_stack[-1][1]
        else:
            return self.torch2trt_kwargs['default_device_type']
        
    def current_device_type_module(self):
        """Returns the module which controls the current device type"""
        if len(self.device_type_stack) > 0:
            return self.device_type_stack[-1][0]
        else:
            return None
        
    def __enter__(self):
        
        # attach hooks which add converters to methods
        for hook in self.hooks:
            hook.__enter__()
        
        # attach hooks which control the current device type
        for name, module in self.torch2trt_kwargs['module'].named_modules():
            pre_hook_handle = module.register_forward_pre_hook(self._module_pre_hook)
            post_hook_handle = module.register_forward_hook(self._module_post_hook)
            self.module_handles.append(pre_hook_handle)
            self.module_handles.append(post_hook_handle)
            
        return self

    def __exit__(self, type, val, tb):
        for hook in self.hooks:
            hook.__exit__(type, val, tb)
        for handle in self.module_handles:
            handle.remove()


    def add_inputs(self, torch_inputs, names=None):
        if names is None:
            names = default_input_names(len(torch_inputs))
        self.input_names = names

        for i, torch_input in enumerate(torch_inputs):
            if not hasattr(torch_input, "_trt"):
                trt_tensor = self.network.add_input(
                    name=names[i],
                    shape=tuple(torch_input.shape),
                    dtype=torch_dtype_to_trt(torch_input.dtype),
                )
                trt_tensor.location = torch_device_to_trt(torch_input.device)
                torch_input._trt = trt_tensor

    def mark_outputs(self, torch_outputs, names=None):
        if names is None:
            names = default_output_names(len(torch_outputs))
        self.output_names = names

        for i, torch_output in enumerate(torch_outputs):
            trt_tensor = torch_output._trt
            trt_tensor.name = names[i]
            trt_tensor.location = torch_device_to_trt(torch_output.device)
            trt_tensor.dtype = torch_dtype_to_trt(torch_output.dtype)
            self.network.mark_output(trt_tensor)


class TRTModule(torch.nn.Module):
    def __init__(self, engine=None, input_names=None, output_names=None):
        super(TRTModule, self).__init__()
        self._register_state_dict_hook(TRTModule._on_state_dict)
        self.engine = engine
        if self.engine is not None:
            self.context = self.engine.create_execution_context()
        self.input_names = input_names
        self.output_names = output_names

    def _on_state_dict(self, state_dict, prefix, local_metadata):
        state_dict[prefix + "engine"] = bytearray(self.engine.serialize())
        state_dict[prefix + "input_names"] = self.input_names
        state_dict[prefix + "output_names"] = self.output_names

    def _load_from_state_dict(
        self,
        state_dict,
        prefix,
        local_metadata,
        strict,
        missing_keys,
        unexpected_keys,
        error_msgs,
    ):
        engine_bytes = state_dict[prefix + "engine"]

        with trt.Logger(min_severity = trt.Logger.INTERNAL_ERROR) as logger, trt.Runtime(logger) as runtime:
            self.engine = runtime.deserialize_cuda_engine(engine_bytes)
            self.context = self.engine.create_execution_context()

        self.input_names = state_dict[prefix + "input_names"]
        self.output_names = state_dict[prefix + "output_names"]

    def forward(self, *inputs):
        batch_size = inputs[0].shape[0]
        bindings = [None] * (len(self.input_names) + len(self.output_names))

        for i, input_name in enumerate(self.input_names):
            idx = self.engine.get_binding_index(input_name)
            shape = tuple(inputs[i].shape)
            bindings[idx] = inputs[i].contiguous().data_ptr()
            self.context.set_binding_shape(idx, shape)

        # create output tensors
        outputs = [None] * len(self.output_names)
        for i, output_name in enumerate(self.output_names):
            idx = self.engine.get_binding_index(output_name)
            dtype = torch_dtype_from_trt(self.engine.get_binding_dtype(idx))
            shape = tuple(self.context.get_binding_shape(idx))
            device = torch_device_from_trt(self.engine.get_location(idx))
            output = torch.empty(size=shape, dtype=dtype, device=device)
            outputs[i] = output
            bindings[idx] = output.data_ptr()


        self.context.execute_async(
            batch_size, bindings, torch.cuda.current_stream().cuda_stream
        )

        outputs = tuple(outputs)
        if len(outputs) == 1:
            outputs = outputs[0]

        return outputs

    def enable_profiling(self):
        if not self.context.profiler:
            self.context.profiler = trt.Profiler()


def torch2trt(module,
              inputs,
              input_names=None,
              output_names=None,
              log_level=trt.Logger.ERROR,
              max_batch_size=1,
              fp16_mode=False,
              max_workspace_size=1<<25,
              strict_type_constraints=False,
              keep_network=True,
              int8_mode=False,
              int8_calib_dataset=None,
              int8_calib_algorithm=DEFAULT_CALIBRATION_ALGORITHM,
              int8_calib_batch_size=1,
              use_onnx=False,
              default_device_type=trt.DeviceType.GPU,
              dla_core=0,
              gpu_fallback=True,
              device_types={},
              **kwargs):

    # capture arguments to provide to context
    kwargs.update(locals())
    kwargs.pop('kwargs')
    inputs_in = inputs

    # copy inputs to avoid modifications to source data
    inputs = [tensor.clone()[0:max_batch_size] for tensor in inputs]  # only run single entry

    logger = trt.Logger(log_level)
    builder = trt.Builder(logger)
    config = builder.create_builder_config()

    if isinstance(inputs, list):
        inputs = tuple(inputs)
    if not isinstance(inputs, tuple):
        inputs = (inputs,)

    # run once to get num outputs
    outputs = module(*inputs)
    if not isinstance(outputs, tuple) and not isinstance(outputs, list):
        outputs = (outputs,)

    if input_names is None:
        input_names = default_input_names(len(inputs))
    if output_names is None:
        output_names = default_output_names(len(outputs))

    if use_onnx:

        f = io.BytesIO()
        torch.onnx.export(module, inputs, f, input_names=input_names, output_names=output_names)
        f.seek(0)
        onnx_bytes = f.read()
        network = builder.create_network(1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
        parser = trt.OnnxParser(network, logger)
        parser.parse(onnx_bytes)

    else:
        network = builder.create_network(1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
        with ConversionContext(network, torch2trt_kwargs=kwargs, builder_config=config) as ctx:

            ctx.add_inputs(inputs, input_names)

            outputs = module(*inputs)

            if not isinstance(outputs, tuple) and not isinstance(outputs, list):
                outputs = (outputs,)
            ctx.mark_outputs(outputs, output_names)


    # set max workspace size
    config.max_workspace_size = max_workspace_size

    if fp16_mode:
        config.set_flag(trt.BuilderFlag.FP16)

    builder.max_batch_size = max_batch_size

    config.default_device_type = default_device_type
    if gpu_fallback:
        config.set_flag(trt.BuilderFlag.GPU_FALLBACK)
    config.DLA_core = dla_core
    
    if strict_type_constraints:
        config.set_flag(trt.BuilderFlag.STRICT_TYPES)

    if int8_mode:

        # default to use input tensors for calibration
        if int8_calib_dataset is None:
            int8_calib_dataset = TensorBatchDataset(inputs_in)

        config.set_flag(trt.BuilderFlag.INT8)

        #Making sure not to run calibration with QAT mode on
        if not 'qat_mode' in kwargs:
            # @TODO(jwelsh):  Should we set batch_size=max_batch_size?  Need to investigate memory consumption
            calibrator = DatasetCalibrator(
                inputs, int8_calib_dataset, batch_size=int8_calib_batch_size, algorithm=int8_calib_algorithm
            )
            config.int8_calibrator = calibrator

    engine = builder.build_engine(network, config)

    module_trt = TRTModule(engine, input_names, output_names)

    if keep_network:
        module_trt.network = network

    return module_trt


# DEFINE ALL CONVERSION FUNCTIONS

def get_module_qualname(name):
    s = name.split('.')

    for i in range(len(s)):
        idx = len(s) - i - 1
        modulename, qualname = ".".join(s[:idx]), ".".join(s[idx:])
        try:
            module = importlib.import_module(modulename)
            return module, modulename, qualname
        except:
            pass

    raise RuntimeError("Could not import module")


def tensorrt_converter(method, is_real=True, enabled=True, imports=[]):

    if isinstance(method, str):
        module, module_name, qual_name = get_module_qualname(method)
    else:
        module, module_name, qual_name = importlib.import_module(method.__module__), method.__module__, method.__qualname__

    try:
        method_impl = eval('copy.deepcopy(module.%s)' % qual_name)
    except:
        enabled = False

    def register_converter(converter):
        CONVERTERS[method] = {
            "converter": converter,
            "is_real": is_real,
            "module": module,
            "module_name": module_name,
            "qual_name": qual_name,
            "method_str": module_name + '.' + qual_name,
            "method_impl": method_impl
        }
        return converter

    def pass_converter(converter):
        return converter

    if enabled:
        return register_converter
    else:
        return pass_converter

    return register_converter


def set_layer_precision(ctx, layer):
    # Supported TRT precisions as given by torch2trt_kwargs.
    INT8_MODE = "int8_mode"
    FP16_MODE = "fp16_mode"

    # Check that args exist as expected in torch2trt_kwargs.
    trt_kwargs = ctx.torch2trt_kwargs
    assert INT8_MODE in trt_kwargs
    assert FP16_MODE in trt_kwargs

    is_int8 = trt_kwargs.get(INT8_MODE, False)
    is_fp16 = trt_kwargs.get(FP16_MODE, False)

    if is_int8:
        layer.precision = trt.int8
        layer.set_output_type(0, trt.int8)
    elif is_fp16:
        layer.precision = trt.float16
        layer.set_output_type(0, trt.float16)

