from collections import namedtuple
#from ..dellcar.parts.keras import KerasPilot
import json
import numpy as np
import pycuda.driver as cuda
import pycuda.autoinit
from pathlib import Path
import tensorflow as tf
import tensorrt as trt
import time
import os
import cv2

HostDeviceMemory = namedtuple('HostDeviceMemory', 'host_memory device_memory')

class TensorRTLinear():
    '''
    Uses TensorRT to do the inference.
    '''
    def __init__(self, *args, **kwargs):
        super(TensorRTLinear, self).__init__(*args, **kwargs)
        self.logger = trt.Logger(trt.Logger.WARNING)
        # self.cfg = cfg
        self.engine = None
        self.inputs = None
        self.outputs = None
        self.bindings = None
        self.stream = None
        self.cfx = cuda.Device(0).make_context()

    def pop(self):
        self.cfx.pop()

    def compile(self):
        print('Nothing to compile')

    def load(self, model_path):
        uff_model = Path(model_path)
        metadata_path = Path('%s/%s.metadata' % (uff_model.parent.as_posix(), uff_model.stem))
        with open(metadata_path.as_posix(), 'r') as metadata, trt.Builder(self.logger) as builder, builder.create_network() as network, trt.UffParser() as parser:
            
            # Without this max_workspace_size setting, I was getting:
            # Building CUDA Engine
            # [TensorRT] ERROR: Internal error: could not find any implementation for node 2-layer MLP, try increasing the workspace size with IBuilder::setMaxWorkspaceSize()
            # [TensorRT] ERROR: ../builder/tacticOptimizer.cpp (1230) - OutOfMemory Error in computeCosts: 0
            builder.max_workspace_size = 1 << 20 #common.GiB(1)
            builder.max_batch_size = 1

            metadata = json.loads(metadata.read())
            # Configure inputs and outputs
            print('Configuring I/O')
            input_names = metadata['input_names']
            output_names = metadata['output_names']
            for name in input_names:
                parser.register_input(name, (3, 240, 320))

            for name in output_names:
                parser.register_output(name)
            # Parse network
            print('Parsing TensorRT Network')
            parser.parse(uff_model.as_posix(), network)
            print('Building CUDA Engine')
            self.engine = builder.build_cuda_engine(network)
            # Allocate buffers
            print('Allocating Buffers')
            self.inputs, self.outputs, self.bindings, self.stream = TensorRTLinear.allocate_buffers(self.engine)
            print('Ready')

    def run(self, image):
        start_time = time.time()
        # Channel first image format
        image = image.transpose((2,0,1))
        # Flatten it to a 1D array.
        image = image.ravel()
        # The first input is the image. Copy to host memory.
        image_input = self.inputs[0] 
        np.copyto(image_input.host_memory, image)
        with self.engine.create_execution_context() as context:
            [throttle, steering] = TensorRTLinear.infer(context=context, bindings=self.bindings, inputs=self.inputs, outputs=self.outputs, stream=self.stream)
            steering  = self.linear_unbin(steering)
            print("used_time: ", time.time() - start_time, "steering: ", steering, "throttle: ", throttle[0])
            return steering, throttle[0]
    
    def linear_unbin(self, arr):
        """
        Convert a categorical array to value.

        See Also
        --------
        linear_bin
        """
        if not len(arr) == 15:
            raise ValueError('Illegal array length, must be 15')
        b = np.argmax(arr)
        a = b * (2 / 14) - 1
        return a

    @classmethod
    def allocate_buffers(cls, engine):
        inputs = []
        outputs = []
        bindings = []
        stream = cuda.Stream()
        for binding in engine:
            size = trt.volume(engine.get_binding_shape(binding)) * engine.max_batch_size
            dtype = trt.nptype(engine.get_binding_dtype(binding))
            # Allocate host and device buffers
            host_memory = cuda.pagelocked_empty(size, dtype)
            device_memory = cuda.mem_alloc(host_memory.nbytes)
            bindings.append(int(device_memory))
            if engine.binding_is_input(binding):
                inputs.append(HostDeviceMemory(host_memory, device_memory))
            else:
                outputs.append(HostDeviceMemory(host_memory, device_memory))

        return inputs, outputs, bindings, stream

    @classmethod
    def infer(cls, context, bindings, inputs, outputs, stream, batch_size=1):
        # Transfer input data to the GPU.
        [cuda.memcpy_htod_async(inp.device_memory, inp.host_memory, stream) for inp in inputs]
        # Run inference.
        context.execute_async(batch_size=batch_size, bindings=bindings, stream_handle=stream.handle)
        # Transfer predictions back from the GPU.
        [cuda.memcpy_dtoh_async(out.host_memory, out.device_memory, stream) for out in outputs]
        # Synchronize the stream
        stream.synchronize()
        # Return only the host outputs.
        return [out.host_memory for out in outputs]

if __name__ == '__main__':
    kl = TensorRTLinear()
    kl.load("/home/mousika/mycar/models/add_model.uff")

    for img in os.listdir("/home/mousika/mycar/unitest/static"):
        if img.endswith("jpg"):
            img = cv2.imread("/home/mousika/mycar/unitest/static/%s"%img)
            kl.run(img)
    kl.pop()

