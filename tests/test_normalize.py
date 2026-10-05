from datetime import datetime

from parsers.normalize import classify_outage, normalize_place, parse_period, tokenize_places


def test_normalize_place_strips_prefixes():
    assert normalize_place("гр. Девня") == "девня"
    assert normalize_place("с. Кости") == "кости"
    assert normalize_place("ОБЩ. ПЕТРИЧ") == "петрич"
    assert normalize_place("Община Дряново") == "дряново"
    assert normalize_place("ж.к. Дружба") == "дружба"


def test_normalize_place_keeps_names_starting_with_prefix_letters():
    # 'с'/'м' must only be stripped with a dot, never inside real names
    assert normalize_place("МЕСТА") == "места"
    assert normalize_place("с. Снежина") == "снежина"
    assert normalize_place("Медово") == "медово"


def test_tokenize_drops_boilerplate_and_stopwords():
    tokens = tokenize_places(
        "ще бъде прекъснато електрозахранването в районите на: "
        "гр. Девня, кв. Повеляново. Проверете за Вашия обект тук"
    )
    assert "девня" in tokens
    assert "повеляново" in tokens
    assert all("проверете" not in t for t in tokens)


def test_parse_period_single_day():
    start, end = parse_period("На 15.06.2026 г. В периода 9:00 ч. до 13:00 ч.")
    assert (start.day, start.hour, start.minute) == (15, 9, 0)
    assert (end.day, end.hour, end.minute) == (15, 13, 0)


def test_parse_period_multi_day():
    start, end = parse_period(
        "От 15.06.2026 г. до 19.06.2026 г. В периода 8:30 ч. до 16:30 ч."
    )
    assert (start.day, start.hour, start.minute) == (15, 8, 30)
    assert (end.day, end.hour, end.minute) == (19, 16, 30)


def test_parse_period_date_digits_not_taken_as_time():
    # "19.06" must not be parsed as 19:06
    start, _ = parse_period("От 15.06.2026 г. до 19.06.2026 г.")
    assert start == datetime(2026, 6, 15, tzinfo=start.tzinfo)
    assert start.hour == 0


def test_classify_outage():
    assert classify_outage("Поради неотложни ремонтни дейности ...") == "planned"
    assert classify_outage("Поради авария в подстанция ...") == "unplanned"
