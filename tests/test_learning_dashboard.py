"""Project totals must retain money provenance and legacy/customer separation."""
from secretary.customer_store import CustomerStore
from secretary.sales_workspace import SalesWorkspace


def test_project_dashboard_keeps_owner_stage_unknown_and_money_type(tmp_path):
    crm = CustomerStore(tmp_path / 'synthetic.sqlite3')
    try:
        sales = SalesWorkspace(crm, clock=lambda: 1800000000)
        customer = crm.create_customer('me', {'name': '合成客户', 'amount_cents': 9000000}, 1800000000)
        private = crm.create_customer('other', {'name': '另一账号'}, 1800000000)
        for name, amount, kind, extra in (
            ('试点估算', 18000000, 'estimate', {}),
            ('已批预算', 46000000, 'budget', {'approval': 'approved'}),
            ('待核金额', None, 'unknown', {}),
            ('明确零元', 0, 'quote', {}),
            ('已赢单', 80000000, 'contract', {'stage': 'won'}),
            ('已丢单', 70000000, 'quote', {'stage': 'lost'}),
            ('已归档', 60000000, 'budget', {'archived': True}),
        ):
            sales.create_opportunity('me', customer['id'], {
                'name': name, 'amount_cents': amount, 'amount_type': kind, **extra})
        sales.create_opportunity('other', private['id'], {'name': '私人项目', 'amount_cents': 90000000})
        result = sales.dashboard_summary('me')
        assert result['project_pipeline_cents'] == 64000000
        assert result['active_projects'] == 4
        assert result['project_unknown_amounts'] == 1
        assert result['project_amount_types'] == {
            'estimate': {'amount_cents': 18000000, 'known_count': 1},
            'budget': {'amount_cents': 46000000, 'known_count': 1},
            'quote': {'amount_cents': 0, 'known_count': 1},
        }
        assert crm.dashboard('me', 1800000000)['stats']['pipeline_cents'] == 9000000
        empty = sales.dashboard_summary('nobody')
        assert empty['active_projects'] == empty['project_pipeline_cents'] == empty['project_unknown_amounts'] == 0
        assert empty['project_amount_types'] == {}
    finally:
        crm.close()
