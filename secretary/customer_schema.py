"""Optional, evidence-backed account and contact attributes for security sales."""


def _fields(group, rows):
    return {key: {"label": label, "group": group, "examples": list(examples)}
            for key, label, examples in rows}


ACCOUNT_FIELDS = {
    **_fields("客户背景", [
        ("legal_name", "法定全称", ["已经核对的单位正式名称"]),
        ("website", "官方网站", ["用户已经核对的网站域名或网址"]),
        ("industry", "行业", ["金融、政务、医疗、能源"]),
        ("region", "区域", ["总部所在地、项目覆盖区域"]),
        ("organization_type", "机构类型", ["国企、民企、事业单位"]),
        ("business_context", "业务与系统背景", ["核心业务、涉及的信息系统"]),
        ("lead_source", "线索来源", ["老客户介绍、展会交流"]),
        ("relationship_status", "合作现状", ["首次接触、已有产品在用"]),
    ]),
    **_fields("安全与密码场景", [
        ("data_scope", "数据范围", ["业务数据类别、流转范围"]),
        ("security_scenarios", "数据安全场景", ["数据分类分级、脱敏、访问审计"]),
        ("existing_systems", "现有系统与供应商", ["已有安全设备、密码设备和使用情况"]),
        ("crypto_needs", "密码应用需求", ["身份鉴别、传输加密、存储加密、签名验签"]),
        ("deployment", "部署环境", ["私有云、物理机、容器、机房"]),
        ("compatibility", "信创与接口适配", ["操作系统、数据库、国产平台及接口要求"]),
        ("compliance_needs", "测评与合规诉求", ["客户提到的密评、等保或数据安全要求"]),
        ("assessment_status", "测评与整改现状", ["计划测评时间、已发现问题和整改进度"]),
    ]),
    **_fields("需求与验收", [
        ("pain_points", "当前痛点", ["客户最想解决的问题及影响"]),
        ("requirements", "需求范围", ["本次项目覆盖哪些系统、哪些能力"]),
        ("success_criteria", "验收标准", ["性能指标、业务验收与测评要求"]),
        ("poc_plan", "测试验证计划", ["测试环境、验证指标及参与人"]),
        ("delivery_constraints", "交付约束", ["上线窗口、停机限制、实施边界"]),
    ]),
    **_fields("商机推进", [
        ("budget_notes", "预算说明", ["预算来源、是否审批、估算范围"]),
        ("procurement_process", "采购流程", ["招投标、集采、采购部门与流程"]),
        ("decision_chain", "决策链", ["谁提需求、谁技术评审、谁审批"]),
        ("timeline", "项目节点", ["立项、测试、采购、上线节点"]),
        ("competition", "竞争情况", ["客户明确提及的其他方案与供应商"]),
        ("blockers", "推进阻力", ["预算、内部协调、技术适配等待解决问题"]),
        ("next_visit_goal", "下次拜访目标", ["核实需求、带技术交流、确认测试计划"]),
    ]),
}

CONTACT_FIELDS = {
    **_fields("联系人画像", [
        ("responsibilities", "工作职责与业务范围", ["负责哪些系统、团队和日常业务；项目权限单独核对"]),
        ("professional_goals", "工作目标与考核重点", ["客户本人明确提及的工作目标、考核压力"]),
        ("authority", "决策影响", ["技术评审人、使用人、采购人、最终审批人"]),
        ("concerns", "关注重点", ["安全效果、交付周期、稳定性、成本"]),
        ("communication_channel", "沟通渠道", ["希望先微信发材料，再电话沟通"]),
        ("contact_hours", "适合联系时段", ["工作日下午方便联系"]),
        ("detail_preference", "材料深度偏好", ["先给一页概览，需要时再补技术细节"]),
        ("interests", "明确提及的喜好", ["用户记录的兴趣或交流偏好"]),
        ("avoidances", "沟通注意事项", ["不在午休时段联系、避免临时改约"]),
        ("relationship_notes", "关系背景", ["通过哪位联系人认识、过往交流背景"]),
    ]),
}

BASIC_LABELS = {
    "name": "客户名称", "contact": "主要联系人", "phone": "主要联系电话",
    "stage": "销售阶段", "amount_cents": "商机金额（分）", "notes": "客户备注",
}
CONTACT_LABELS = {"name": "联系人姓名", "role": "职务角色", "phone": "联系电话", "department": "部门"}

# Project-specific discovery must never be inferred as a company-wide fact.
# Existing account keys remain readable for historical, explicitly entered data.
PROJECT_FIELDS = {
    key: ACCOUNT_FIELDS[key] for key in (
        "pain_points", "requirements", "success_criteria", "poc_plan", "delivery_constraints",
        "budget_notes", "procurement_process", "decision_chain", "timeline", "competition",
        "blockers", "next_visit_goal", "existing_systems", "compatibility", "deployment",
        "compliance_needs", "assessment_status", "data_scope", "security_scenarios", "crypto_needs")
}
PROJECT_FIELDS.update(_fields("项目成交要素", [
    ("business_value", "业务影响与量化价值", ["当前问题导致的损失、节省的工时、风险降低目标"]),
    ("budget_approval", "预算来源与审批状态", ["预算来自哪个部门、是否批准、审批还差哪一步"]),
    ("decision_process", "决策与采购步骤", ["技术评审、预算审批、采购流程、签约步骤"]),
    ("champion", "内部支持者与协作条件", ["明确愿意推动本项目的人及其需要的支持"]),
    ("veto_risks", "否决风险与反对意见", ["明确反对的原因、必需通过的关口"]),
]))

ACCOUNT_BACKGROUND_KEYS = frozenset({
    "legal_name", "website", "industry", "region", "organization_type", "business_context", "lead_source", "relationship_status"})
