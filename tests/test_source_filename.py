import pytest

from historian_relay_bot.utils.source_filename import (
    build_author_part,
    build_filename,
    build_short_title,
    build_surname,
    is_valid_filename_part,
    normalize_for_dup,
    parse_authors,
    strip_diacritics,
)

# (authors, is_edited, title, year, topic_code, expected_filename)
FIXTURES = [
    (
        ["Adam Tooze"], False,
        "The Wages of Destruction: The Making and Breaking of the Nazi Economy",
        2006, "ECON",
        "WW2-ECON_Tooze_2006_Wages-of-Destruction.pdf",
    ),
    (
        ["Karl-Heinz Frieser"], False,
        "The Blitzkrieg Legend: The 1940 Campaign in the West",
        2005, "WEST",
        "WW2-WEST_Frieser_2005_Blitzkrieg-Legend.pdf",
    ),
    (
        ["Joseph A. Maiolo"], False,
        "The Royal Navy and Nazi Germany, 1933–39: A Study in Appeasement…",
        1998, "ORIG",
        "WW2-ORIG_Maiolo_1998_Royal-Navy-and-Nazi-Germany.pdf",
    ),
    (
        ["David Stahel"], False,
        "Operation Typhoon: Hitler's March on Moscow, October 1941",
        2013, "EAST",
        "WW2-EAST_Stahel_2013_Operation-Typhoon.pdf",
    ),
    (
        ["Ian Kershaw"], False,
        "To Hell and Back: Europe 1914–1949",
        2015, "GEN",
        "WW2-GEN_Kershaw_2015_To-Hell-and-Back.pdf",
    ),
    (
        ["Mark Harrison"], True,
        "The Economics of World War II: Six Great Powers in International Comparison",
        1998, "ECON",
        "WW2-ECON_Harrison-ed_1998_Economics-of-World-War-II.pdf",
    ),
]


@pytest.mark.parametrize("authors,is_edited,title,year,topic_code,expected", FIXTURES)
def test_known_fixtures(authors, is_edited, title, year, topic_code, expected):
    author_part = build_author_part(authors, is_edited=is_edited)
    short_title_part = build_short_title(title)
    filename = build_filename(
        prefix="WW2-", topic_code=topic_code, author_part=author_part,
        year=year, short_title_part=short_title_part, ext="pdf",
    )
    assert filename == expected


def test_two_authors():
    assert build_author_part(["Richard Overy", "Andrew Wheatcroft"], is_edited=False) == "Overy-Wheatcroft"


def test_three_or_more_authors():
    assert build_author_part(["Alan Smith", "Barbara Jones", "Carl Davis"], is_edited=False) == "Smith-etal"


def test_diacritics():
    assert strip_diacritics("Müller") == "Muller"
    assert build_surname("Walther Müller") == "Muller"


def test_apostrophes_are_stripped_not_space_replaced():
    assert build_short_title("People's History of the World: A Survey") == "Peoples-History-of-the-World"


def test_title_with_no_colon_uses_whole_title():
    assert build_short_title("A Short History of Nearly Everything") == "Short-History-of-Nearly-Everything"


def test_parse_authors_splits_on_semicolon_and_strips():
    assert parse_authors("Adam Tooze; Richard Overy ;  Andrew Wheatcroft ") == [
        "Adam Tooze", "Richard Overy", "Andrew Wheatcroft",
    ]


def test_parse_authors_empty():
    assert parse_authors("") == []
    assert parse_authors(";;;") == []


def test_is_valid_filename_part():
    assert is_valid_filename_part("Smith-Jones")
    assert is_valid_filename_part("Tooze")
    assert not is_valid_filename_part("Smith Jones")
    assert not is_valid_filename_part("-Smith")
    assert not is_valid_filename_part("Smith-")
    assert not is_valid_filename_part("")


def test_normalize_for_dup_ignores_article_case_and_punctuation():
    assert normalize_for_dup("The Wages of Destruction") == normalize_for_dup("wages, of destruction!")
