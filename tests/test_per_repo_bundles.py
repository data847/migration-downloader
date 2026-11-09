"""Standalone per-repo bundles: each is the combined download's inner <target>.zip, on its own."""

import hashlib
import io
import zipfile

import pytest

import bundle
from errors import MigrationError
from test_hardening import make_run

TAR_BYTES = b"A" * 50


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inner_of_combined(run, tmp_path, name):
    built = bundle.build(run, tmp_dir=tmp_path)
    try:
        with zipfile.ZipFile(built) as outer:
            return outer.read(name)
    finally:
        built.unlink()


@pytest.fixture
def client(monkeypatch):
    import app
    return app.app.test_client()


class TestBundleModule:
    def test_names_lists_every_target_and_run_files_last(self, tmp_path):
        run = make_run(tmp_path)
        assert bundle.names(run) == ["acme-api.zip", "acme-web.zip", "ws-orphan.zip", "run-files.zip"]

    def test_standalone_bundle_is_byte_identical_to_the_combined_inner_zip(self, tmp_path):
        run = make_run(tmp_path)
        for name in bundle.names(run):
            one = bundle.build_one(run, name, tmp_dir=tmp_path)
            try:
                assert one.read_bytes() == inner_of_combined(run, tmp_path, name)
            finally:
                one.unlink()

    def test_contents_tarball_unchanged_for_check_and_extras_layout(self, tmp_path):
        run = make_run(tmp_path)
        one = bundle.build_one(run, "acme-api.zip", tmp_dir=tmp_path)
        try:
            with zipfile.ZipFile(one) as z:
                assert sorted(z.namelist()) == ["acme-api-1.for-check.csv", "acme-api-1.tar.gz",
                                                "extras/acme-api/hooks.json", "extras/acme-api/sub/log.log"]
                assert z.getinfo("acme-api-1.tar.gz").compress_type == zipfile.ZIP_STORED   # never recompressed
                assert sha(z.read("acme-api-1.tar.gz")) == sha((run / "acme-api-1.tar.gz").read_bytes())
                assert z.read("acme-api-1.for-check.csv") == (run / "acme-api-1.for-check.csv").read_bytes()
        finally:
            one.unlink()

    def test_extras_only_target_is_handled_like_the_combined_bundle(self, tmp_path):
        run = make_run(tmp_path)
        one = bundle.build_one(run, "ws-orphan.zip", tmp_dir=tmp_path)
        try:
            assert zipfile.ZipFile(one).namelist() == ["extras/ws-orphan/refs.json"]
        finally:
            one.unlink()

    def test_run_files_is_separate_and_holds_the_manifest(self, tmp_path):
        run = make_run(tmp_path)
        one = bundle.build_one(run, "run-files.zip", tmp_dir=tmp_path)
        try:
            assert zipfile.ZipFile(one).namelist() == ["manifest.json"]
        finally:
            one.unlink()

    def test_unknown_or_traversing_names_are_refused(self, tmp_path):
        run = make_run(tmp_path)
        for bad in ("../x.zip", "/etc/passwd", "acme-api", "nope.zip", "..\\x.zip", ""):
            with pytest.raises(MigrationError):
                bundle.build_one(run, bad, tmp_dir=tmp_path)

    def test_it_checks_disk_space(self, tmp_path, monkeypatch):
        run = make_run(tmp_path)
        monkeypatch.setattr(bundle, "check_disk", lambda *a, **k: (_ for _ in ()).throw(MigrationError("only 1 MB free")))
        with pytest.raises(MigrationError, match="MB free"):
            bundle.build_one(run, "acme-api.zip", tmp_dir=tmp_path)
        with pytest.raises(MigrationError, match="MB free"):
            bundle.write_all(run)

    def test_by_archive_maps_archive_files_to_their_bundle(self, tmp_path):
        run = make_run(tmp_path)
        assert bundle.by_archive(run) == {"acme-api-1.tar.gz": "acme-api.zip", "acme-web-2.tar.gz": "acme-web.zip"}


class TestWriteAll:
    def test_writes_beside_the_archives_and_leaves_originals_alone(self, tmp_path):
        run = make_run(tmp_path)
        before = {p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}
        written, unchanged, conflicts = bundle.write_all(run, log=lambda _m: None)
        assert sorted(p.name for p in written) == ["acme-api.zip", "acme-web.zip", "run-files.zip", "ws-orphan.zip"]
        assert unchanged == [] and conflicts == []
        for name, data in before.items():
            assert (run / name).read_bytes() == data
        assert (run / "acme-api-1.tar.gz").read_bytes() == TAR_BYTES
        assert not list(run.glob(".md-bundle-*"))                      # no temp leftovers
        for p in written:
            assert zipfile.ZipFile(p).testzip() is None

    def test_standalone_files_equal_the_combined_inner_zips(self, tmp_path):
        run = make_run(tmp_path)
        expected = {n: inner_of_combined(run, tmp_path, n) for n in bundle.names(run)}
        bundle.write_all(run, log=lambda _m: None)
        for name, data in expected.items():
            assert (run / name).read_bytes() == data

    def test_second_run_is_a_no_op_and_generated_files_stay_out_of_the_combined_zip(self, tmp_path):
        run = make_run(tmp_path)
        first = bundle.build(run, tmp_dir=tmp_path)
        with zipfile.ZipFile(first) as z:
            names_before = z.namelist()
        first.unlink()
        bundle.write_all(run, log=lambda _m: None)
        written, unchanged, conflicts = bundle.write_all(run, log=lambda _m: None)
        assert written == [] and conflicts == [] and len(unchanged) == 4
        built = bundle.build(run, tmp_dir=tmp_path)
        try:
            with zipfile.ZipFile(built) as z:
                assert z.namelist() == names_before
                with zipfile.ZipFile(io.BytesIO(z.read("run-files.zip"))) as rf:
                    assert rf.namelist() == ["manifest.json"]
        finally:
            built.unlink()

    def test_a_different_existing_file_is_never_overwritten(self, tmp_path):
        run = make_run(tmp_path)
        (run / "acme-web.zip").write_bytes(b"someone else's file")
        logs = []
        written, _unchanged, conflicts = bundle.write_all(run, log=logs.append)
        assert [p.name for p in conflicts] == ["acme-web.zip"]
        assert (run / "acme-web.zip").read_bytes() == b"someone else's file"
        assert "acme-web.zip" not in [p.name for p in written] and any("left alone" in m for m in logs)
        assert not list(run.glob(".md-bundle-*"))

    def test_a_symlink_at_the_destination_is_not_followed(self, tmp_path):
        run = make_run(tmp_path)
        victim = tmp_path / "victim.txt"
        victim.write_text("keep")
        (run / "acme-web.zip").symlink_to(victim)
        _w, _u, conflicts = bundle.write_all(run, log=lambda _m: None)
        assert [p.name for p in conflicts] == ["acme-web.zip"] and victim.read_text() == "keep"

    def test_a_failed_write_leaves_no_temp_file(self, tmp_path, monkeypatch):
        run = make_run(tmp_path)
        monkeypatch.setattr(bundle.zipfile.ZipFile, "write", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
        with pytest.raises(OSError):
            bundle.write_all(run, log=lambda _m: None)
        assert not list(run.glob(".md-bundle-*")) and not (run / "acme-api.zip").exists()

    def test_an_empty_run_has_nothing_to_bundle(self, tmp_path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(MigrationError, match="nothing to bundle"):
            bundle.write_all(tmp_path / "empty")


class TestCli:
    def _main(self, monkeypatch, run, argv, failed=0):
        import cli
        monkeypatch.setattr(cli, "run_job", lambda spec, log=print: {"failed": failed, "output_dir": str(run)})
        return cli.main(argv)

    def test_flag_writes_the_standalone_zips_into_the_run_folder(self, tmp_path, monkeypatch):
        run = make_run(tmp_path)
        assert self._main(monkeypatch, run, ["bitbucket", "ws/r", "--per-repo-bundles"]) == 0
        assert (run / "acme-api.zip").is_file() and (run / "run-files.zip").is_file()
        assert (run / "acme-api-1.tar.gz").read_bytes() == TAR_BYTES

    def test_without_the_flag_nothing_is_written(self, tmp_path, monkeypatch):
        run = make_run(tmp_path)
        assert self._main(monkeypatch, run, ["bitbucket", "ws/r"]) == 0
        assert not (run / "acme-api.zip").exists()

    def test_a_conflict_makes_the_exit_code_nonzero(self, tmp_path, monkeypatch):
        run = make_run(tmp_path)
        (run / "acme-api.zip").write_bytes(b"x")
        assert self._main(monkeypatch, run, ["bitbucket", "ws/r", "--per-repo-bundles"]) == 1
        assert (run / "acme-api.zip").read_bytes() == b"x"

    def test_the_flag_is_in_the_help(self, capsys):
        import cli
        with pytest.raises(SystemExit):
            cli.main(["--help"])
        assert "--per-repo-bundles" in capsys.readouterr().out


class TestEndpoint:
    def test_one_bundle_is_served_and_equals_the_combined_inner_zip(self, client, tmp_path, monkeypatch):
        import app
        run = make_run(tmp_path)
        monkeypatch.setattr(app, "_run_root", lambda name: run)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        monkeypatch.setattr(bundle.tempfile, "gettempdir", lambda: str(scratch))
        reply = client.get("/api/archive-bundle/run1/acme-api.zip")
        assert reply.status_code == 200 and reply.mimetype == "application/zip"
        assert "acme-api.zip" in reply.headers["Content-Disposition"]
        body = reply.get_data()
        reply.close()
        assert body == inner_of_combined(run, tmp_path, "acme-api.zip")
        assert list(scratch.iterdir()) == []

    def test_run_files_is_served_separately(self, client, tmp_path, monkeypatch):
        import app
        run = make_run(tmp_path)
        monkeypatch.setattr(app, "_run_root", lambda name: run)
        reply = client.get("/api/archive-bundle/run1/run-files.zip")
        assert zipfile.ZipFile(io.BytesIO(reply.get_data())).namelist() == ["manifest.json"]
        reply.close()

    def test_listing_gives_names_and_archive_mapping(self, client, tmp_path, monkeypatch):
        import app
        run = make_run(tmp_path)
        monkeypatch.setattr(app, "_run_root", lambda name: run)
        data = client.get("/api/archive-bundle/run1").get_json()
        assert data["bundles"][-1] == "run-files.zip" and data["by_archive"]["acme-web-2.tar.gz"] == "acme-web.zip"

    def test_unknown_names_and_traversal_are_404(self, client, tmp_path, monkeypatch):
        import app
        run = make_run(tmp_path)
        monkeypatch.setattr(app, "_run_root", lambda name: run)
        for bad in ("nope.zip", "..%2Fmanifest.json", "manifest.json", "acme-api-1.tar.gz", "%2e%2e"):
            assert client.get(f"/api/archive-bundle/run1/{bad}").status_code == 404

    def test_dot_runs_are_refused_by_the_real_run_resolver(self, client):
        assert client.get("/api/archive-bundle/.jobs/x.zip").status_code == 404

    def test_disk_shortage_is_a_clear_507(self, client, tmp_path, monkeypatch):
        import app
        run = make_run(tmp_path)
        monkeypatch.setattr(app, "_run_root", lambda name: run)
        monkeypatch.setattr(bundle, "check_disk", lambda *a, **k: (_ for _ in ()).throw(MigrationError("only 1 MB free")))
        reply = client.get("/api/archive-bundle/run1/acme-api.zip")
        assert reply.status_code == 507 and "MB free" in reply.get_json()["error"]

    def test_the_page_has_the_per_repo_and_run_files_actions(self, client):
        html = client.get("/").get_data(as_text=True)
        assert "Download bundle" in html and "run-files.zip" in html and "/api/archive-bundle/" in html


class TestGarmrLayout:
    """Garmr (batch._analyse_zip) opens a .zip, pairs each *.tar.gz with the *.for-check.csv at
    the same level inside it, and ignores other members. Checked by layout, without importing Garmr."""

    def test_tarball_and_for_check_sit_at_the_zip_root_with_matching_stems(self, tmp_path):
        run = make_run(tmp_path)
        one = bundle.build_one(run, "acme-api.zip", tmp_dir=tmp_path)
        try:
            names = zipfile.ZipFile(one).namelist()
        finally:
            one.unlink()
        tars = [n for n in names if "/" not in n and n.endswith(".tar.gz")]
        csvs = [n for n in names if "/" not in n and n.endswith(".for-check.csv")]
        assert tars == ["acme-api-1.tar.gz"] and csvs == ["acme-api-1.for-check.csv"]
        assert csvs[0][: -len(".for-check.csv")] == tars[0][: -len(".tar.gz")]
