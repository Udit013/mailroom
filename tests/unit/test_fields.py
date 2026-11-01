from datetime import date

import pytest

from mailroom.fields import Fields, extract_fields, normalize_case_number


@pytest.mark.parametrize(
    "line",
    [
        "Case No. FIC-2026-001",
        "Case Number: FIC-2026-001",
        "Matter: FIC-2026-001",
        "CASE # FIC-2026-001",
    ],
)
def test_case_number_labels(line):
    assert extract_fields(line).case_numbers == ("FIC-2026-001",)


def test_case_numbers_that_differ_only_in_formatting_are_one_number():
    fields = extract_fields("Matter: fic-2026-001\nCase No. FIC-2026-001")

    assert len(fields.case_numbers) == 1
    assert not fields.conflicting


def test_a_word_after_the_label_is_not_a_case_number():
    assert extract_fields("In this matter: please respond").case_numbers == ()


def test_normalization_ignores_case_and_punctuation():
    assert normalize_case_number("fic 2026/001") == normalize_case_number("FIC-2026-001")


def test_nothing_is_invented_for_an_empty_document():
    fields = extract_fields("Nothing labelled here.")

    assert fields == Fields()
    assert not fields.conflicting


def test_two_matters_are_a_conflict():
    assert extract_fields("Matter: FIC-1\nMatter: FIC-2").conflicting


def test_two_clients_are_a_conflict():
    assert extract_fields("Client: Aldermere\nClient: Brightwater").conflicting


def test_the_same_client_twice_is_not_a_conflict():
    assert not extract_fields("Client: Aldermere\nClient: ALDERMERE").conflicting


def test_opposing_parties_are_not_a_conflict():
    fields = extract_fields("Plaintiff: Aldermere\nDefendant: Brightwater")

    assert fields.parties == ("Aldermere", "Brightwater")
    assert not fields.conflicting


def test_caption_names_both_parties():
    assert extract_fields("Aldermere Holdings v. Brightwater Cooperative").parties == (
        "Aldermere Holdings",
        "Brightwater Cooperative",
    )


@pytest.mark.parametrize(
    "line", ["Date: 2026-03-04", "Dated: March 4, 2026", "Date: Mar 4, 2026", "Date: 4 March 2026"]
)
def test_unambiguous_dates_are_parsed(line):
    assert extract_fields(line).document_date == date(2026, 3, 4)


def test_a_date_that_could_mean_two_things_is_left_missing():
    assert extract_fields("Date: 03/04/2026").document_date is None


def test_two_dates_are_reported_rather_than_chosen():
    fields = extract_fields("Date: 2026-03-04\nDate: 2026-05-06")

    assert fields.document_date is None
    assert fields.date_conflict


def test_sender_comes_from_the_label():
    assert extract_fields("From: Registry Office\nBody").sender == "Registry Office"


def test_two_senders_are_left_missing():
    assert extract_fields("From: Registry\nFrom: Clinic").sender is None


def test_output_survives_a_round_trip():
    fields = extract_fields("Matter: FIC-1\nClient: Aldermere\nDate: 2026-03-04\nFrom: Registry")

    assert Fields.from_output(fields.to_output()) == fields


def test_repeated_values_are_capped():
    text = "\n".join(f"Matter: FIC-{number}" for number in range(100))

    assert len(extract_fields(text).case_numbers) == 20
