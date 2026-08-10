import os
import sys
import types

sys.path.insert(0, os.path.dirname(__file__))


def _stub_module(name: str, **attrs):
    """
    Stub out heavy optional dependencies (faster-whisper, llama-cpp-python,
    google-api-python-client) that sermon_pipeline.py imports at module level
    but that aren't needed to exercise its pure functions. Real environments
    (CI, the worker AMI) have these installed for real; this just lets the
    unit test suite run without them.
    """
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


_stub_module("googleapiclient")
_stub_module("googleapiclient.discovery", build=lambda *a, **k: None)
_stub_module("faster_whisper", WhisperModel=object)
_stub_module("llama_cpp", Llama=object)
