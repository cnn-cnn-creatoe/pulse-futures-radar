"""Shared score gate; missing evidence never satisfies a positive minimum."""
import math
RULE_ID='wt_extreme_cross'

def passes_scores(scores, thresholds):
    action=scores.get('actionability') or {}
    for value,minimum in ((scores.get('activity_score'),thresholds.get('min_activity',0)),
                          (action.get('score'),thresholds.get('min_actionability',0))):
        if minimum>0 and (isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or value<minimum):
            return False
    return True
