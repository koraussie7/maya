"""torch<2.3 호환 shim.

transformers 4.52.3 은 torch 2.3+ 에서 추가된 ``torch.get_default_device()`` 를 호출한다.
voicemem 은 transformers==4.52.3 을 하드 핀하는데, Intel Mac 에서는 torch 2.2.2 가
마지막으로 안정적인 wheel 이라 그대로 쓰면 E5 모델 로드 시
``AttributeError: module 'torch' has no attribute 'get_default_device'`` 로 죽는다.

torch 를 올리면 다른 스택이 깨질 수 있으므로, 없을 때만 CPU 를 돌려주는 함수를 붙인다.
(이 환경은 CPU 추론만 하므로 정확히 동일한 의미다.)
"""
import torch

if not hasattr(torch, "get_default_device"):
    def get_default_device() -> "torch.device":
        return torch.device("cpu")

    torch.get_default_device = get_default_device  # type: ignore[attr-defined]
