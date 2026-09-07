import os

from anomaly_detection_engine.config import load_dotenv


def test_load_dotenv_sets_a_variable_not_already_present(tmp_path, monkeypatch):
    monkeypatch.delenv("FAKE_TEST_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("FAKE_TEST_KEY=from-dotenv\n", encoding="utf-8")

    load_dotenv(env_file)

    assert os.environ["FAKE_TEST_KEY"] == "from-dotenv"


def test_load_dotenv_never_overrides_an_existing_real_env_var(tmp_path, monkeypatch):
    # A real environment variable (the shell, CI, a process manager)
    # must always win over whatever the file says -- the standard
    # dotenv precedence, and the reason load_dotenv() uses setdefault().
    monkeypatch.setenv("FAKE_TEST_KEY", "from-shell")
    env_file = tmp_path / ".env"
    env_file.write_text("FAKE_TEST_KEY=from-dotenv\n", encoding="utf-8")

    load_dotenv(env_file)

    assert os.environ["FAKE_TEST_KEY"] == "from-shell"


def test_load_dotenv_skips_comments_and_blank_lines(tmp_path, monkeypatch):
    monkeypatch.delenv("FAKE_TEST_KEY", raising=False)
    monkeypatch.delenv("FAKE_TEST_KEY_2", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n# a full-line comment\nFAKE_TEST_KEY=one\n\nFAKE_TEST_KEY_2=two\n",
        encoding="utf-8",
    )

    load_dotenv(env_file)

    assert os.environ["FAKE_TEST_KEY"] == "one"
    assert os.environ["FAKE_TEST_KEY_2"] == "two"


def test_load_dotenv_strips_one_layer_of_matching_quotes(tmp_path, monkeypatch):
    monkeypatch.delenv("FAKE_TEST_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('FAKE_TEST_KEY="quoted value"\n', encoding="utf-8")

    load_dotenv(env_file)

    assert os.environ["FAKE_TEST_KEY"] == "quoted value"


def test_load_dotenv_ignores_a_line_with_no_equals_sign(tmp_path, monkeypatch):
    monkeypatch.delenv("FAKE_TEST_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("this line has no equals sign\nFAKE_TEST_KEY=ok\n", encoding="utf-8")

    load_dotenv(env_file)

    assert os.environ["FAKE_TEST_KEY"] == "ok"


def test_load_dotenv_does_nothing_when_the_file_is_missing(tmp_path):
    # Must not raise -- .env is entirely optional.
    load_dotenv(tmp_path / "does-not-exist.env")
