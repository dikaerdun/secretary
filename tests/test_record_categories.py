from secretary.crm import CRMStore


def test_channels_are_classified_and_still_owner_isolated(tmp_path):
    crm = CRMStore(tmp_path / 'crm.sqlite3')
    try:
        idea = crm.capture_message('a', 'v1', '我有个想法，可以把产品演示做成短视频。', 'voice', 100)
        meeting = crm.create_record('a', {'title': '会议纪要', 'content': '今天项目会议讨论交付范围。'}, 101)
        visit = crm.capture_message('a', 'v2', '拜访复盘：我感觉王总还有预算顾虑。', 'voice', 102)
        conversation = crm.capture_message('b', 'v3', '客户王总说希望先看测试方案。', 'text', 103)
        assert idea['category'] == 'idea'
        assert meeting['category'] == 'meeting'
        assert visit['category'] == 'visit_review'
        assert conversation['category'] == 'conversation'
        assert [r['id'] for r in crm.list_records('a', category='idea')['items']] == [idea['id']]
        assert crm.list_records('a', category='conversation')['total'] == 0
        assert crm._db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    finally:
        crm.close()


def test_category_correction_is_preserved_by_replay_and_children(tmp_path):
    crm = CRMStore(tmp_path / 'crm.sqlite3')
    try:
        note = crm.capture_message('a', 'same', '随口记下一件事', 'voice', 100)
        changed = crm.update_record('a', note['id'], {'category': 'idea'}, 101)
        assert changed['original_content'] == '随口记下一件事'
        assert crm.capture_message('a', 'same', '重复回调', 'voice', 102)['category'] == 'idea'
        child = crm.create_record('a', {'title': '验证想法', 'parent_record_id': note['id'], 'kind': 'action'}, 103)
        assert child['category'] == 'idea'
    finally:
        crm.close()


def test_category_validation_and_reopen_preserve_existing_data(tmp_path):
    import pytest
    path = tmp_path / 'crm.sqlite3'
    crm = CRMStore(path)
    note = crm.create_record('a', {'title': '原记录', 'content': '客户交流', 'category': 'conversation'}, 100)
    with pytest.raises(ValueError):
        crm.update_record('a', note['id'], {'category': 'invalid'}, 101)
    with pytest.raises(ValueError):
        crm.list_records('a', category="idea' OR 1=1")
    crm.close()
    reopened = CRMStore(path)
    try:
        assert reopened.get_record('a', note['id'])['category'] == 'conversation'
        assert reopened._db.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        reopened.close()
