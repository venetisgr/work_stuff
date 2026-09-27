import pytest

from sharepoint_digest.config import PROJECT_ROOT, ConfigError, find_folder, load_folders, load_settings


def test_the_shipped_folder_list_has_the_four_placeholders():
    folders = load_folders(PROJECT_ROOT / "folders.toml")
    assert [(f.key, f.label, f.path) for f in folders] == [
        (f"temp-folder-{n}", f"Temp Folder {n}", f"Temp Folder {n}") for n in range(1, 5)
    ]


def test_folders_can_override_site_and_library(tmp_path):
    path = tmp_path / "folders.toml"
    path.write_text(
        '[folders.board]\nlabel = "Board packs"\npath = "/Board/2026/"\n'
        'site_url = "https://contoso.sharepoint.com/sites/Exec"\nlibrary = "Board Documents"\n'
    )
    [folder] = load_folders(path)
    assert (folder.path, folder.site_url, folder.library) == (
        "Board/2026",
        "https://contoso.sharepoint.com/sites/Exec",
        "Board Documents",
    )


def test_a_local_path_makes_the_sharepoint_path_optional(tmp_path):
    path = tmp_path / "folders.toml"
    # Single quotes keep Windows backslashes as they are.
    path.write_text("[folders.board]\nlocal_path = 'C:\\Users\\me\\OneDrive - Contoso\\Board'\n")
    [folder] = load_folders(path)
    assert (folder.label, folder.path, folder.local_path) == ("board", "", r"C:\Users\me\OneDrive - Contoso\Board")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "doesn't define any folders"),
        ("[folders.a]\nlabel = 'A'\n", 'Folder "a" .* needs a path'),
        ("[folders.a\n", "not valid TOML"),
    ],
)
def test_bad_folder_lists_are_explained(tmp_path, content, message):
    path = tmp_path / "folders.toml"
    path.write_text(content)
    with pytest.raises(ConfigError, match=message):
        load_folders(path)


def test_find_folder_by_key_label_or_number():
    folders = load_folders(PROJECT_ROOT / "folders.toml")
    assert find_folder(folders, "temp-folder-2").key == "temp-folder-2"
    assert find_folder(folders, "temp folder 3").key == "temp-folder-3"
    assert find_folder(folders, "4").key == "temp-folder-4"
    with pytest.raises(ConfigError, match="Choose one of: temp-folder-1"):
        find_folder(folders, "finance")


def test_settings_defaults():
    settings = load_settings({"FOUNDRY_DEPLOYMENT": " gpt-4.1 ", "FOUNDRY_REASONING_EFFORT": "LOW"})
    assert settings.sharepoint.library == "Documents"
    assert settings.sharepoint.auth_mode == "app"
    assert settings.foundry.deployment == "gpt-4.1"
    assert settings.foundry.reasoning_effort == "low"
    assert settings.foundry.api_key is None
    assert settings.foundry.max_output_tokens is None
    assert settings.max_input_chars == 200_000


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"GRAPH_AUTH_MODE": "password"}, "GRAPH_AUTH_MODE must be one of"),
        ({"LLM_MAX_INPUT_CHARS": "lots"}, "must be a whole number"),
        ({"LLM_MAX_OUTPUT_TOKENS": "0"}, "greater than zero"),
    ],
)
def test_invalid_settings_are_explained(env, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(env)
