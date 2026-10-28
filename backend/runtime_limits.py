"""Server-owned chat budgets; clients cannot increase them."""
from dataclasses import dataclass
import os


def bounded(name,default,low,high):
    value=int(os.getenv(name,str(default)))
    if not low<=value<=high:
        raise ValueError(name+' outside supported bounds')
    return value


@dataclass(frozen=True)
class ChatLimits:
    total_seconds: int=120
    model_calls: int=4
    output_tokens: int=4096

    @classmethod
    def from_env(cls):
        return cls(bounded('CHAT_TOTAL_TIMEOUT_SECONDS',120,10,300),
                   bounded('CHAT_MAX_MODEL_CALLS',4,1,8),bounded('CHAT_MAX_OUTPUT_TOKENS',4096,256,16000))
