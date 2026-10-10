"""Empty compatibility registry for saved replay infrastructure; custom rules retired."""
from pathlib import Path

class IndicatorRegistryError(ValueError):pass
class IndicatorValidationError(ValueError):
    errors=['自定义指标已移除']
class CustomIndicatorRegistry:
    def __init__(self,path=None):self.path=Path(path) if path else None
    def list(self):return []
    def get(self,indicator_id):return None

def evaluate_indicator(*args,**kwargs):
    raise IndicatorValidationError('自定义指标已移除，请使用 WaveTrend 超买超卖交叉')
