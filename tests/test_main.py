from unittest.mock import patch


def test_create_root_uses_plain_tk():
    import main

    fallback_root = object()
    with patch.object(main.tk, "Tk", return_value=fallback_root) as mock_tk:
        assert main.create_root() is fallback_root

    mock_tk.assert_called_once_with()
