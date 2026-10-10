"""Inventory for the unified installed rules."""
def inventory():
    from .community_indicators import CATALOG
    return dict(items=[dict(**{**r,'kind':'indicator'},source_id=r['id'],source_state='available',ported=True,status='已接入',likes=None) for r in CATALOG],excluded=0,skipped_items=[],source_summary={'indicator':dict(total=len(CATALOG),available=len(CATALOG),pending=0,skipped=0),'strategy':dict(total=0,available=0,pending=0,skipped=0)},source_complete=True,complete=True,note='统一指标策略研究库；自创策略保留原生开平仓逻辑。')
