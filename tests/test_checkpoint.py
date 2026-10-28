from mailforge.infrastructure.checkpoint import read_checkpoint, write_checkpoint


def test_checkpoint_round_trip_and_replacement(tmp_path):
    path = tmp_path / "state" / "checkpoint.json"
    write_checkpoint(path, {"position": 4, "completed": False})
    assert read_checkpoint(path) == {"position": 4, "completed": False}
    write_checkpoint(path, {"position": 5, "completed": True})
    assert read_checkpoint(path) == {"position": 5, "completed": True}
    assert not list(path.parent.glob(".*checkpoint.json.*"))


def test_invalid_checkpoint_is_not_promoted_to_state(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_text("not-json", encoding="utf-8")
    assert read_checkpoint(path) is None
