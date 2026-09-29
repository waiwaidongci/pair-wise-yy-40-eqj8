from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='建筑抗震鉴定与加固排序'; ENTITY='抗震鉴定'; ID_PREFIX='SR'
SEVERITIES=['low', 'medium', 'high', 'severe']; STATES=['proposed', 'assessed', 'design', 'construction', 'accepted', 'rejected']; TRANSITIONS={'proposed': ['assessed'], 'assessed': ['design', 'rejected'], 'design': ['construction'], 'construction': ['accepted'], 'accepted': ['rejected'], 'rejected': []}; TRANSITION_ROLES={'assessed': ['assessor'], 'design': ['structural_engineer'], 'construction': ['structural_engineer'], 'accepted': ['review_board'], 'rejected': ['review_board']}
CREATE_ROLES=set(['assessor']); RECORD_ROLES=set(['assessor', 'structural_engineer']); AUDIT_ROLES=set(['review_board', 'viewer']); VIEW_ROLES=set(['assessor', 'structural_engineer', 'review_board', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'medium': 3.0, 'high': 6.0, 'severe': 9.0}; DEADLINE_HOURS={'low': 72, 'medium': 24, 'high': 8, 'severe': 4}; TERMINAL_STATES=set(['accepted', 'rejected'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))

# ---- 结构依赖（共用连廊/传力体系）----
# 依赖边：upstream_item_id（上游，先加固）-> downstream_item_id（下游，后加固）
CONSTRUCTION_STATE='construction'
DEPENDENCY_BASIS_PENDING='pending'      # 上游尚未验收，依据仍有效但未满足
DEPENDENCY_BASIS_SATISFIED='satisfied'  # 上游已验收，依据满足
DEPENDENCY_BASIS_INVALID='invalid'      # 上游被驳回，依据失效
# 已开工指进入施工或已验收；处于proposed/assessed/design/rejected均视为未开工
STARTED_STATES=set(['construction','accepted'])
DEPENDENCY_ROLES=set(['assessor','structural_engineer'])

def require_item_id(value):
    if isinstance(value,bool): raise ValidationError("项目ID必须是正整数")
    if not isinstance(value,int): raise ValidationError("项目ID必须是正整数")
    if value<1: raise ValidationError("项目ID必须是正整数")
    return value

def has_started_construction(status):
    return status in STARTED_STATES

def dependency_basis_status(upstream_status):
    """上游状态决定共用部位依据的效力。"""
    if upstream_status=='accepted': return DEPENDENCY_BASIS_SATISFIED
    if upstream_status=='rejected': return DEPENDENCY_BASIS_INVALID
    return DEPENDENCY_BASIS_PENDING

def construction_basis_blockers(dependencies):
    """下游进入施工前，必须全部上游验收；返回未满足的依赖说明。"""
    blockers=[]
    for dep in dependencies:
        state=dependency_basis_status(dep["upstream_status"])
        if state!=DEPENDENCY_BASIS_SATISFIED:
            blockers.append({"upstream_item_id":dep["upstream_item_id"],
                             "upstream_title":dep.get("upstream_title"),
                             "shared_part":dep.get("shared_part"),
                             "basis":dep.get("basis"),
                             "upstream_status":dep["upstream_status"],
                             "basis_status":state})
    return blockers

def invalid_basis_for_not_started(dependencies, downstream_status):
    """上游被驳回后：仅对未开工下游列出失效依据；已开工的保留记录但不告警。"""
    if has_started_construction(downstream_status): return []
    return [{"upstream_item_id":dep["upstream_item_id"],
             "upstream_title":dep.get("upstream_title"),
             "shared_part":dep.get("shared_part"),
             "basis":dep.get("basis"),
             "upstream_status":dep["upstream_status"],
             "basis_status":DEPENDENCY_BASIS_INVALID}
            for dep in dependencies
            if dependency_basis_status(dep["upstream_status"])==DEPENDENCY_BASIS_INVALID]

def find_cycle(edges, new_upstream, new_downstream):
    """加入new_upstream->new_downstream后是否成环，成环返回回路（id列表，首尾闭合）。
    edges: 既有边[(upstream_id, downstream_id), ...]。自环视为回路。"""
    if new_upstream==new_downstream:
        return [new_upstream,new_downstream]
    adjacency={}
    for up,down in edges:
        adjacency.setdefault(up,set()).add(down)
    adjacency.setdefault(new_upstream,set()).add(new_downstream)
    visited=set()
    def dfs(node,path):
        if node in path:
            return path[path.index(node):]+[node]
        if node in visited: return None
        path.append(node)
        for nxt in adjacency.get(node,()):
            found=dfs(nxt,path)
            if found is not None: return found
        path.pop(); visited.add(node); return None
    return dfs(new_upstream,[])
