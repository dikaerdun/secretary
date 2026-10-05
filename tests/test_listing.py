from secretary.store import Store


def test_long_personal_list_fits_wecom_message_and_can_page(tmp_path):
    store = Store(tmp_path / 'list.sqlite3')
    try:
        for index in range(25):
            store.execute('me', f'create-{index}', {
                'action': 'create', 'title': '测' * 120, 'remind_at': None,
            }, 1800000000)
        first = store.execute('me', 'list-1', {'action': 'list'}, 1800000000)
        second = store.execute('me', 'list-2', {'action': 'list', 'page': 2}, 1800000000)
        assert len(first.encode('utf-8')) < 16000
        assert '#20 ' in first and '#21 ' not in first
        assert '#21 ' in second and '#25 ' in second
        assert '待办 2' in first
    finally:
        store.close()
