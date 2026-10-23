from historian_relay_bot.utils.formatting import build_source_post_text


def test_basic_format_without_file_or_link():
    text = build_source_post_text(
        title="The Wages of Destruction: The Making and Breaking of the Nazi Economy",
        authors=["Adam Tooze"],
        is_edited=False,
        publisher="Penguin",
        year=2006,
        original_year=None,
        topic="ECON",
        secondary_topic=None,
        covers="Germany, 1933–1945",
        work_type="Academic monograph",
        filename=None,
        link=None,
        submitted_by_user_id=123456789,
    )
    expected = (
        "\U0001F4D6 **The Wages of Destruction: The Making and Breaking of the Nazi Economy**\n"
        "**Author:** Adam Tooze\n"
        "**Published:** Penguin, 2006\n"
        "**Topic:** ECON\n"
        "**Covers:** Germany, 1933–1945\n"
        "**Type:** Academic monograph\n"
        "**Submitted by:** <@123456789>"
    )
    assert text == expected


def test_edited_volume_uses_editor_label():
    text = build_source_post_text(
        title="The Economics of World War II", authors=["Mark Harrison"], is_edited=True,
        publisher="Cambridge University Press", year=1998, original_year=None,
        topic="ECON", secondary_topic=None, covers="Six great powers compared",
        work_type="Edited volume", filename=None, link=None, submitted_by_user_id=1,
    )
    assert "**Editor:** Mark Harrison" in text
    assert "**Author:**" not in text


def test_original_year_secondary_topic_file_and_link_all_shown():
    text = build_source_post_text(
        title="Some Title", authors=["A Author", "B Author"], is_edited=False,
        publisher="Some Press", year=2020, original_year=1955,
        topic="WEST", secondary_topic="EAST", covers="Europe",
        work_type="Reference work", filename="WW2-WEST_Author-Author_2020_Some-Title.pdf",
        link="https://example.com/book", submitted_by_user_id=42,
    )
    assert "**Published:** Some Press, 2020 (original 1955)" in text
    assert "**Topic:** WEST · also EAST" in text
    assert "**File:** WW2-WEST_Author-Author_2020_Some-Title.pdf" in text
    assert "**Link:** https://example.com/book" in text
    assert text.startswith("\U0001F4D6 **Some Title**")
    assert text.endswith("**Submitted by:** <@42>")


def test_file_and_link_omitted_when_absent():
    text = build_source_post_text(
        title="T", authors=["A"], is_edited=False, publisher="P", year=2000,
        original_year=None, topic="GEN", secondary_topic=None, covers="X",
        work_type="Article", filename=None, link=None, submitted_by_user_id=1,
    )
    assert "**File:**" not in text
    assert "**Link:**" not in text


def test_multiple_authors_joined_with_comma():
    text = build_source_post_text(
        title="T", authors=["A B", "C D", "E F"], is_edited=False,
        publisher="P", year=2000, original_year=None, topic="GEN",
        secondary_topic=None, covers="X", work_type="Article",
        filename=None, link=None, submitted_by_user_id=1,
    )
    assert "**Author:** A B, C D, E F" in text
