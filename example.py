# Variables que se pueden exportar
# TVM_USE_CMSIS (bool)
# TVM_MICRO_BOARD (string) - ek_ra8m1
# 
import os
import pathlib
import tarfile
import tempfile
import shutil

import tensorflow as tf
import numpy as np

import tvm
from tvm import relay
from tvm.relay.backend import Executor, Runtime
from tvm.contrib.download import download_testdata
from tvm.micro import export_model_library_format
import tvm.micro.testing
from tvm.micro.testing.utils import (
    create_header_file,
    mlf_extract_workspace_size_bytes,
)

MODEL_INDEX = 4#2

# Model params
MODEL_SHORT_NAME = "VWW"
MODEL_URL = "https://github.com/mlcommons/tiny/raw/bceb91c5ad2e2deb295547d81505721d3a87d578/benchmark/training/visual_wake_words/trained_models/vww_96_int8.tflite"
MODEL_FILE_NAME = "vww_96_int8.tflite"

if MODEL_INDEX == 1:
    MODEL_SHORT_NAME = "KWS"
    MODEL_URL = "https://github.com/mlcommons/tiny/raw/bceb91c5ad2e2deb295547d81505721d3a87d578/benchmark/training/keyword_spotting/trained_models/kws_ref_model.tflite"
    MODEL_FILE_NAME = "kws_ref_model.tflite"
elif MODEL_INDEX == 3:
    MODEL_SHORT_NAME = "AD"
    MODEL_URL = "https://github.com/mlcommons/tiny/raw/bceb91c5ad2e2deb295547d81505721d3a87d578/benchmark/training/anomaly_detection/trained_models/ad01_fp32.tflite"
    MODEL_FILE_NAME = "ad01_fp32.tflite"
elif MODEL_INDEX == 4:
    MODEL_SHORT_NAME = "IC"
    MODEL_URL = "https://github.com/mlcommons/tiny/raw/bceb91c5ad2e2deb295547d81505721d3a87d578/benchmark/training/image_classification/trained_models/pretrainedResnet_quant.tflite"
    MODEL_FILE_NAME = "pretrainedResnet_quant.tflite"

MODEL_PATH = download_testdata(MODEL_URL, MODEL_FILE_NAME, module="model")

# Use 'export USE_CMSIS=1' to use CMSIS-NN
USE_CMSIS = os.environ.get("TVM_USE_CMSIS", False)

tflite_model_buf = open(MODEL_PATH, "rb").read()

import tflite

tflite_model = tflite.Model.GetRootAsModel(tflite_model_buf, 0)

interpreter = tf.lite.Interpreter(model_path=str(MODEL_PATH))
interpreter.allocate_tensors()
input_details = interpreter.get_input_details()
output_details = interpreter.get_output_details()

input_name = input_details[0]["name"]
input_shape = tuple(input_details[0]["shape"])
input_dtype = np.dtype(input_details[0]["dtype"]).name
output_name =output_details[0]["name"]
output_shape = tuple(output_details[0]["shape"])
output_dtype = np.dtype(output_details[0]["dtype"]).name

# We extract quantization information from TFLite model.
# This is required for all models except Anomaly Detection,
# because for other models we send quantized data to interpreter
# from host, however, for AD model we send floating data and quantization
# happens on the microcontroller.
if MODEL_SHORT_NAME != "AD":
    quant_output_scale = output_details[0]["quantization_parameters"]["scales"][0]
    quant_output_zero_point = output_details[0]["quantization_parameters"]["zero_points"][0]

relay_mod, params = relay.frontend.from_tflite(
    tflite_model, shape_dict={input_name: input_shape}, dtype_dict={input_name: input_dtype}
)

# Use the C runtime (crt)
RUNTIME = Runtime("crt") # AoT case
# RUNTIME = Runtime("crt", {"system-lib": True}) # Graph case

# Use the AoT executor with `unpacked-api=True` and `interface-api=c`. `interface-api=c` forces
# the compiler to generate C type function APIs and `unpacked-api=True` forces the compiler
# to generate minimal unpacked format inputs which reduces the stack memory usage on calling
# inference layers of the model.
EXECUTOR = Executor(
    "aot",
    # "graph", {"link-params": True}, # Creo que graph podría destrozar el benchmarking, no usar
    {"unpacked-api": True, "interface-api": "c", "workspace-byte-alignment": 8},
)

# Select a Zephyr board (export TVM_MICRO_BOARD = tuplacafavorita) 
BOARD = os.getenv("TVM_MICRO_BOARD", default="nucleo_h743zi") # ek_ra8m1

# Get the full target description using the BOARD
TARGET = tvm.micro.testing.get_target("zephyr", BOARD)

config = {"tir.disable_vectorize": True} 
if USE_CMSIS:
    from tvm.relay.op.contrib import cmsisnn

    config["relay.ext.cmsisnn.options"] = {"mcpu": TARGET.mcpu}
    relay_mod = cmsisnn.partition_for_cmsisnn(relay_mod, params, mcpu=TARGET.mcpu)

with tvm.transform.PassContext(opt_level=3, config=config):
    module = tvm.relay.build(
        relay_mod, target=TARGET, params=params, runtime=RUNTIME, executor=EXECUTOR
    )

temp_dir = tvm.contrib.utils.tempdir()
model_tar_path = temp_dir / "model.tar"
export_model_library_format(module, model_tar_path)
workspace_size = mlf_extract_workspace_size_bytes(model_tar_path)

extra_tar_dir = tvm.contrib.utils.tempdir()
extra_tar_file = extra_tar_dir / "extra.tar"

with tarfile.open(extra_tar_file, "w:gz") as tf:
    create_header_file(
        "output_data",
        np.zeros(
            shape=output_shape,
            dtype=output_dtype,
        ),
        "include/tvm",
        tf,
    )

input_total_size = 1
for i in range(len(input_shape)):
    input_total_size *= input_shape[i]

template_project_path = pathlib.Path(tvm.micro.get_microtvm_template_projects("zephyr"))
project_options = {
    "extra_files_tar": str(extra_tar_file),
    "project_type": "mlperftiny",
    "board": BOARD,
    "compile_definitions": [
        f"-DWORKSPACE_SIZE={workspace_size + 512}", 
        # Memory workspace, 512 is a temporary offset
        # since the memory calculation is not accurate.

        f"-DTARGET_MODEL={MODEL_INDEX}", # Model index for compilation
        f"-DTH_MODEL_VERSION=EE_MODEL_VERSION_{MODEL_SHORT_NAME}01", # As required by MLPerfTiny API
        f"-DMAX_DB_INPUT_SIZE={input_total_size}", # Max size of the input data array
    ],
}


if MODEL_SHORT_NAME != "AD":
    project_options["compile_definitions"].append(f"-DOUT_QUANT_SCALE={quant_output_scale}")
    project_options["compile_definitions"].append(f"-DOUT_QUANT_ZERO={quant_output_zero_point}")

if USE_CMSIS:
    project_options["compile_definitions"].append(f"-DCOMPILE_WITH_CMSISNN=1")


# Note: to be adjusted depending on the target board
project_options["config_main_stack_size"] = 4000

if USE_CMSIS:
    project_options["cmsis_path"] = os.environ.get("CMSIS_PATH", "/content/cmsis")

generated_project_dir = temp_dir / "project"

project = tvm.micro.project.generate_project_from_mlf(
    template_project_path, generated_project_dir, model_tar_path, project_options
)
project.build()

if(BOARD == "nucleo_h743zi"):
    with open(f'{generated_project_dir}/build/CMakeCache.txt', 'a') as file:
        file.write('ZEPHYR_BOARD_FLASH_RUNNER:STRING=openocd\n')
elif(BOARD == "ek_ra8m1"):
    with open(f'{generated_project_dir}/build/CMakeCache.txt', 'a') as file:
        file.write('ZEPHYR_BOARD_FLASH_RUNNER:STRING=jlink\n')

#Clean the BUILD directory and extra stuff
shutil.rmtree(generated_project_dir / "build")
(generated_project_dir / "model.tar").unlink()

project_tar_path = pathlib.Path(os.getcwd()) / "project.tar"
with tarfile.open(project_tar_path, "w:tar") as tar:
    tar.add(generated_project_dir, arcname=os.path.basename("project"))

print(f"The generated project is located here: {project_tar_path}")
