from ebayspy.ebay import EbayClient


def test_extract_item_id_from_listing_path() -> None:
    url = "https://www.ebay.com/itm/Test-Item/123456789012?hash=item"

    assert EbayClient._extract_item_id(url) == "123456789012"


def test_extract_item_id_from_query_param() -> None:
    url = "https://www.ebay.com/p/whatever?item=987654321098&foo=bar"

    assert EbayClient._extract_item_id(url) == "987654321098"


def test_clean_description_collapses_whitespace() -> None:
    assert EbayClient._clean_description("A   nice\n\nitem\tlisted today") == "A nice item listed today"

