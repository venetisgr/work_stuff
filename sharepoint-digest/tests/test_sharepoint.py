from datetime import UTC, datetime

import pytest
from conftest import (
    SITE_URL,
    FakeResponse,
    children_url,
    content_url,
    fake_graph,
    graph_file,
    graph_folder,
    site_routes,
)

from sharepoint_digest.config import ConfigError, SharePointSettings
from sharepoint_digest.sharepoint import GRAPH_URL, GraphError, SharePointFolder, graph_credential

START = datetime(2026, 9, 1, tzinfo=UTC)
END = datetime(2026, 10, 1, tzinfo=UTC)


def folder_routes() -> dict:
    routes = site_routes()
    routes[children_url("folder-1")] = {
        "value": [
            graph_file("f-new", "Q3 plan.pptx", "2026-09-20T10:00:00Z"),
            graph_file("f-old", "Q1 plan.pptx", "2026-03-02T10:00:00Z"),
            graph_file("f-xlsx", "Budget.xlsx", "2026-09-05T10:00:00Z"),
            graph_file("f-lock", "~$Q3 plan.pptx", "2026-09-21T10:00:00Z"),
        ],
        "@odata.nextLink": children_url("folder-1") + "?$skiptoken=page2",
    }
    routes[children_url("folder-1") + "?$skiptoken=page2"] = {
        "value": [
            graph_folder("sub-1", "Minutes"),
            graph_file("f-legacy", "Old deck.ppt", "2026-09-03T10:00:00Z"),
        ]
    }
    routes[children_url("sub-1")] = {
        "value": [graph_file("f-docx", "Kickoff.docx", "2026-09-02T08:30:00Z", created="2026-08-15T08:30:00Z")]
    }
    return routes


def resolve(routes: dict, library: str = "Documents", path: str = "Temp Folder 1"):
    graph, session, credential = fake_graph(routes)
    return SharePointFolder.resolve(graph, SITE_URL, library, path), session, credential


def test_resolve_finds_the_site_library_and_folder():
    folder, session, credential = resolve(site_routes(), library="Shared Documents")

    assert (folder.drive_id, folder.item_id) == ("drive-docs", "folder-1")
    assert all(call["headers"] == {"Authorization": "Bearer token-1"} for call in session.calls)
    assert credential.tokens_issued == 1  # the token is reused until it nears expiry


def test_unknown_library_lists_the_ones_that_exist():
    with pytest.raises(ConfigError, match="Libraries on that site: Archive, Documents"):
        resolve(site_routes(), library="Reports")


def test_missing_folder_points_at_folders_toml():
    with pytest.raises(ConfigError, match='Folder "Nope" not found.*folders.toml'):
        resolve(site_routes(), path="Nope")


def test_site_url_must_be_https():
    graph, _, _ = fake_graph({})
    with pytest.raises(ConfigError, match="should look like"):
        SharePointFolder.resolve(graph, "contoso.sharepoint.com/sites/Team", "Documents", "")


def test_list_files_filters_by_date_and_type_and_walks_subfolders_and_pages():
    folder, _, _ = resolve(folder_routes())

    listing = folder.list_files(START, END)

    assert [(f.path, f.kind) for f in listing.files] == [
        ("Minutes/Kickoff.docx", "Word document"),
        ("Q3 plan.pptx", "PowerPoint deck"),
    ]
    assert [(s.file.name, s.reason) for s in listing.skipped] == [
        ("Old deck.ppt", ".ppt files aren't supported; save it as .pptx or .docx to include it.")
    ]
    kickoff = listing.files[0]
    assert kickoff.modified_by == "Dana Scully"
    assert kickoff.modified == datetime(2026, 9, 2, 8, 30, tzinfo=UTC)


def test_list_files_can_skip_subfolders_and_filter_on_created_date():
    folder, _, _ = resolve(folder_routes())

    assert [f.name for f in folder.list_files(START, END, recursive=False).files] == ["Q3 plan.pptx"]
    # Kickoff.docx was modified in September but created in August.
    assert [f.name for f in folder.list_files(START, END, date_field="created").files] == ["Q3 plan.pptx"]


def test_download_returns_the_file_bytes():
    routes = folder_routes()
    routes[content_url("f-new")] = b"pptx bytes"
    folder, _, _ = resolve(routes)

    q3_plan = next(f for f in folder.list_files(START, END).files if f.name == "Q3 plan.pptx")
    assert folder.download(q3_plan) == b"pptx bytes"


def test_permission_errors_explain_the_graph_permissions_needed():
    url = f"{GRAPH_URL}/sites/contoso.sharepoint.com:/sites/Team"
    denied = FakeResponse(url, 403, {"error": {"code": "accessDenied", "message": "Access denied"}})
    with pytest.raises(GraphError, match="Access denied.*Sites.Read.All") as info:
        resolve({url: denied})
    assert info.value.status == 403


def test_app_auth_mode_needs_tenant_client_and_secret():
    settings = SharePointSettings(SITE_URL, "Documents", "app", "tenant", None, None)
    with pytest.raises(ConfigError, match="AZURE_CLIENT_ID, AZURE_CLIENT_SECRET"):
        graph_credential(settings)
