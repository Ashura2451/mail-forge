import json
from pathlib import Path

from mailforge.application.sorting import AddressValidator, Checkpoint, InputReader
from mailforge.application.transform import (
    format_email_file,
    remove_duplicate_lines,
    write_legacy_list,
)
from mailforge.infrastructure.dns import DNSService
from mailforge.infrastructure.providers import ProviderCatalog


def test_canonical_transformations_preserve_file_contract(tmp_path):
    source = tmp_path / "emails.txt"
    formatted = tmp_path / "formatted_emails.txt"
    listed = tmp_path / "sorted_mails.txt"
    source.write_text("a@example.com\n\na@example.com\nb@example.com\n", encoding="utf-8")
    format_email_file(source, formatted)
    write_legacy_list(formatted, listed)
    assert json.loads(listed.read_text(encoding="utf-8").replace("'", '"')) == [
        "a@example.com",
        "a@example.com",
        "b@example.com",
    ]
    assert remove_duplicate_lines(formatted) == 1
    assert formatted.read_text(encoding="utf-8") == "a@example.com\nb@example.com\n"


def test_canonical_sorting_input_validation_checkpoint_and_provider_routing(tmp_path):
    txt = tmp_path / "input.txt"
    csv_path = tmp_path / "input.csv"
    json_path = tmp_path / "input.json"
    jsonl = tmp_path / "input.jsonl"
    txt.write_text("A@example.com and invalid\n", encoding="utf-8")
    csv_path.write_text("email\nB@example.com\n", encoding="utf-8")
    json_path.write_text('["C@example.com"]', encoding="utf-8")
    jsonl.write_text('{"email": "D@example.com"}\n', encoding="utf-8")
    assert list(InputReader.iter_emails(txt)) == ["A@example.com"]
    assert list(InputReader.iter_emails(csv_path)) == ["B@example.com"]
    assert list(InputReader.iter_emails(json_path)) == ["C@example.com"]
    assert list(InputReader.iter_emails(jsonl)) == ["D@example.com"]

    providers = Path(__file__).parents[1] / "MSORTER" / "config" / "providers.json"
    assert ProviderCatalog(providers).route("x@gmail.com")[0] == "gmail.txt"
    validator = AddressValidator()
    assert validator.validate("x@example.com")[0]

    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint = Checkpoint(checkpoint_path)
    assert checkpoint.save(str(txt), 12, 1, False)
    assert Checkpoint(checkpoint_path).can_resume(str(txt)) == (True, 12, 1)


def test_canonical_dns_behavior_uses_a_mocked_resolver(tmp_path):
    class Answer:
        ttl = 300
        exchange = "aspmx.l.google.com."
        preference = 10

    class Resolver:
        def resolve(self, domain, record_type):
            assert (domain, record_type) == ("example.com", "MX")
            return [Answer()]

    service = DNSService(sync_resolver=Resolver())
    response = service.resolve_one_sync("example.com")
    assert response.provider_id == "google_workspace"
    assert service.resolve_one_sync("example.com") is response
