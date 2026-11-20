import asyncio
from types import SimpleNamespace

import discord

from ebayspy.discord_bot import (
    BUTTON_LABEL_LIMIT,
    COLOUR_AUCTION,
    COLOUR_DEAL,
    COLOUR_ENDING_SOON,
    COLOUR_LOT,
    COLOUR_OFFER,
    CONTENT_LIMIT,
    CUSTOM_ID_LIMIT,
    EMBED_DESCRIPTION_LIMIT,
    SLASH_COMMANDS,
    TEXT_COMMANDS,
    USER_COMMANDS,
    Button,
    DiscordBot,
    DiscordError,
    _reply_target,
    _ReplyTarget,
    escape_md,
)
from ebayspy.models import Listing, MarketItem


def _bot(global_id: str = "EBAY-GB", **kwargs) -> DiscordBot:
    return DiscordBot("token", ebay_global_id=global_id, **kwargs)


class _FakePartial:
    def __init__(self, sink: list) -> None:
        self.sink = sink

    async def edit(self, **kwargs) -> None:
        self.sink.append(kwargs)


class _FakeChannel:
    """Records what the bot sends/edits, standing in for a discord channel."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.next_id = 100

    async def send(self, **kwargs) -> SimpleNamespace:
        self.sent.append(kwargs)
        self.next_id += 1
        return SimpleNamespace(id=self.next_id)

    def get_partial_message(self, message_id: int) -> _FakePartial:
        self.edits.append({"message_id": message_id})
        return _FakePartial(self.edits)


def _with_fake_channel(bot: DiscordBot) -> _FakeChannel:
    channel = _FakeChannel()

    async def fake_channel(channel_id: str):
        return channel

    bot._channel = fake_channel  # type: ignore[method-assign]
    return channel


def _listing(**overrides) -> Listing:
    base = dict(
        item_id="1",
        seller="colourfu59",
        title="Dyson Airblade HU02",
        price="430.00 GBP",
        url="https://www.ebay.co.uk/itm/1",
        listed_at="2026-06-13T09:36:52.000Z",
        seller_feedback_percent="98.6",
        seller_feedback_score="1004",
        image_url="https://i.ebayimg.com/images/g/abc/s-l1600.jpg",
    )
    base.update(overrides)
    return Listing(**base)


def _item(**overrides) -> MarketItem:
    base = dict(
        item_id="1",
        title="Dyson Airblade HU02",
        url="https://www.ebay.co.uk/itm/1",
        seller="bargain22",
        currency="GBP",
        item_price=300.0,
        total_price=309.99,
        shipping_cost=9.99,
        seller_feedback_percent=99.2,
        seller_feedback_score=540,
        buying_options=("FIXED_PRICE", "BEST_OFFER"),
    )
    base.update(overrides)
    return MarketItem(**base)


# ----- markdown escaping ------------------------------------------------------


def test_escape_md_protects_underscores_but_leaves_urls_alone() -> None:
    assert escape_md("seller_one and *bold*") == r"seller\_one and \*bold\*"
    url = "https://i.ebayimg.com/images/g/ab_cd/s-l1600.jpg"
    assert escape_md(f"see {url} now_") == rf"see {url} now\_"


def test_escape_md_escapes_line_leading_markers_without_stray_backslashes() -> None:
    # A list line indented like the /watches output must not gain "\   -".
    assert escape_md("#3 dyson\n   -15% · every 10m") == "\\#3 dyson\n   \\-15% · every 10m"
    assert escape_md("> quoted") == r"\> quoted"
    assert escape_md("a - b") == "a - b"  # mid-line hyphen is not a list marker


# ----- buttons / views ----------------------------------------------------------


def test_build_view_lays_buttons_out_in_rows_with_discord_styles() -> None:
    rows = [
        [Button("🔗 Open", url="https://www.ebay.co.uk/itm/1")],
        [Button("Mute", "mv:1:2"), Button("Not it", "bl:1:2", style="danger")],
        [Button("👍", "fu:1:2", style="success"), Button("👎", "fd:1:2")],
    ]
    components = DiscordBot.build_view(rows).to_components()
    assert [len(row["components"]) for row in components] == [1, 2, 2]
    link = components[0]["components"][0]
    assert link["style"] == discord.ButtonStyle.link.value and link["url"].endswith("/itm/1")
    assert "custom_id" not in link
    mute, not_it = components[1]["components"]
    assert mute["custom_id"] == "mv:1:2" and mute["style"] == discord.ButtonStyle.secondary.value
    assert not_it["custom_id"] == "bl:1:2" and not_it["style"] == discord.ButtonStyle.danger.value
    assert components[2]["components"][0]["style"] == discord.ButtonStyle.success.value


def test_build_view_clips_to_discord_limits() -> None:
    long_label = "x" * (BUTTON_LABEL_LIMIT + 20)
    long_id = "rs:" + "a" * (CUSTOM_ID_LIMIT + 20)
    rows = [[Button(long_label, long_id)] * 7] * 7  # too many per row and too many rows
    components = DiscordBot.build_view(rows).to_components()
    assert len(components) == 5
    assert all(len(row["components"]) == 5 for row in components)
    button = components[0]["components"][0]
    assert len(button["label"]) == BUTTON_LABEL_LIMIT
    assert len(button["custom_id"]) == CUSTOM_ID_LIMIT


# ----- text payloads ------------------------------------------------------------


def test_text_payload_uses_content_when_short_and_embed_when_long() -> None:
    content, embed = DiscordBot._text_payload("Added seller_one.")
    assert content == r"Added seller\_one." and embed is None

    long_text = "\n".join(f"line {i}" for i in range(2000))
    content, embed = DiscordBot._text_payload(long_text)
    assert content is None
    assert embed is not None and len(embed.description) <= EMBED_DESCRIPTION_LIMIT
    assert embed.description.endswith("…")


def test_send_message_posts_to_the_channel_and_returns_message_id() -> None:
    bot = _bot()
    channel = _with_fake_channel(bot)
    try:
        message_id = asyncio.run(bot.send_message("123", "hello_world", [[Button("x", "rs:x")]]))
        assert message_id == 101
        sent = channel.sent[0]
        assert sent["content"] == r"hello\_world"
        assert "embed" not in sent
        assert sent["view"].to_components()[0]["components"][0]["custom_id"] == "rs:x"
    finally:
        asyncio.run(bot.close())


def test_send_message_over_content_limit_goes_out_as_embed() -> None:
    bot = _bot()
    channel = _with_fake_channel(bot)
    try:
        asyncio.run(bot.send_message("123", "x" * (CONTENT_LIMIT + 500)))
        sent = channel.sent[0]
        assert "content" not in sent
        assert len(sent["embed"].description) <= EMBED_DESCRIPTION_LIMIT
    finally:
        asyncio.run(bot.close())


def test_edit_message_replaces_text_and_buttons_in_place() -> None:
    bot = _bot()
    channel = _with_fake_channel(bot)
    try:
        asyncio.run(bot.edit_message("123", 42, "Watching:\nchfle-6052", [[Button("🗑", "rs:c")]]))
        assert channel.edits[0] == {"message_id": 42}
        edit = channel.edits[1]
        assert edit["content"] == "Watching:\nchfle-6052" and edit["embed"] is None
        assert edit["view"].to_components()[0]["components"][0]["custom_id"] == "rs:c"

        # An empty button list clears the components; None leaves them untouched.
        asyncio.run(bot.edit_message("123", 42, "No sellers yet.", []))
        assert channel.edits[-1]["view"].to_components() == []
        asyncio.run(bot.edit_message("123", 42, "No sellers yet."))
        assert "view" not in channel.edits[-1]
    finally:
        asyncio.run(bot.close())


def test_channel_rejects_non_numeric_ids_without_logging_in() -> None:
    bot = _bot()
    try:
        try:
            asyncio.run(bot._channel("chat-1"))
            raise AssertionError("expected DiscordError")
        except DiscordError as exc:
            assert "chat-1" in str(exc)
        assert not bot._logged_in
    finally:
        asyncio.run(bot.close())


# ----- slash-command reply routing ------------------------------------------------


class _FakeResponse:
    def __init__(self) -> None:
        self.deferred: list[dict] = []
        self.sent: list[tuple] = []

    async def defer(self, **kwargs) -> None:
        self.deferred.append(kwargs)

    async def send_message(self, *args, **kwargs) -> None:
        self.sent.append((args, kwargs))


class _FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, *args, **kwargs) -> SimpleNamespace:
        if args:
            kwargs["content"] = args[0]
        self.sent.append(kwargs)
        return SimpleNamespace(id=555)


def _interaction(channel_id: int = 123, **overrides) -> SimpleNamespace:
    interaction = SimpleNamespace(
        type=discord.InteractionType.application_command,
        channel_id=channel_id,
        user=SimpleNamespace(name="rob435", id=1),
        message=None,
        data={},
        response=_FakeResponse(),
        followup=_FakeFollowup(),
        deleted=0,
    )

    async def delete_original_response() -> None:
        interaction.deleted += 1

    interaction.delete_original_response = delete_original_response
    for key, value in overrides.items():
        setattr(interaction, key, value)
    return interaction


def test_dispatch_slash_defers_then_answers_with_the_handler_reply() -> None:
    bot = _bot()
    seen: list[tuple] = []

    async def handler(channel_id, username, command, arg):
        seen.append((channel_id, username, command, arg))
        return "Added seller: lithosale."

    bot._handler = handler
    interaction = _interaction()
    try:
        asyncio.run(bot._dispatch_slash(interaction, "add", "  lithosale [100] "))
        assert interaction.response.deferred == [{"thinking": True}]
        assert seen == [("123", "rob435", "/add", "lithosale [100]")]
        assert interaction.followup.sent == [{"content": "Added seller: lithosale."}]
        assert interaction.deleted == 0
    finally:
        asyncio.run(bot.close())


def test_dispatch_slash_routes_a_table_sent_by_the_handler_as_the_response() -> None:
    # /list makes the service send the list itself (with buttons) and return "".
    # That send must become the interaction's response, not a loose post, and the
    # "thinking…" placeholder must not be deleted out from under it.
    bot = _bot()
    channel = _with_fake_channel(bot)

    async def handler(channel_id, username, command, arg):
        await bot.send_message(channel_id, "Watching:\nlithosale", [[Button("🗑", "rs:lithosale")]])
        return ""

    bot._handler = handler
    interaction = _interaction()
    try:
        asyncio.run(bot._dispatch_slash(interaction, "list", ""))
        assert len(interaction.followup.sent) == 1
        sent = interaction.followup.sent[0]
        assert sent["wait"] is True and sent["content"] == "Watching:\nlithosale"
        assert sent["view"].to_components()[0]["components"][0]["custom_id"] == "rs:lithosale"
        assert channel.sent == []  # nothing went to the channel directly
        assert interaction.deleted == 0
    finally:
        asyncio.run(bot.close())


def test_dispatch_slash_drops_placeholder_when_handler_has_nothing_to_say() -> None:
    bot = _bot()

    async def handler(channel_id, username, command, arg):
        return ""

    bot._handler = handler
    interaction = _interaction()
    try:
        asyncio.run(bot._dispatch_slash(interaction, "check", ""))
        assert interaction.followup.sent == []
        assert interaction.deleted == 1
    finally:
        asyncio.run(bot.close())


def test_reply_target_only_captures_sends_to_the_invoking_channel() -> None:
    bot = _bot()
    channel = _with_fake_channel(bot)
    interaction = _interaction(channel_id=123)
    target = _ReplyTarget(interaction)  # type: ignore[arg-type]
    token = _reply_target.set(target)
    try:
        # An alert to some other channel during a command must still go to that channel.
        asyncio.run(bot.send_message("999", "elsewhere"))
        assert channel.sent[0]["content"] == "elsewhere" and not target.used
        # The first send to the invoking channel becomes the response; a second
        # one goes to the channel as a normal message.
        asyncio.run(bot.send_message("123", "first"))
        asyncio.run(bot.send_message("123", "second"))
        assert interaction.followup.sent[0]["content"] == "first" and target.used
        assert channel.sent[1]["content"] == "second"
    finally:
        _reply_target.reset(token)
        asyncio.run(bot.close())


# ----- text commands ---------------------------------------------------------------


def _message(content: str, *, bot_author: bool = False, mentions=()) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        author=SimpleNamespace(bot=bot_author, name="rob435"),
        channel=SimpleNamespace(id=123),
        mentions=list(mentions),
    )


def test_on_message_answers_known_text_commands_only() -> None:
    bot = _bot()
    channel = _with_fake_channel(bot)
    seen: list[tuple] = []

    async def handler(channel_id, username, command, arg):
        seen.append((channel_id, username, command, arg))
        return "ok"

    bot._handler = handler
    try:
        asyncio.run(bot._on_message(_message("!add lithosale [100]")))
        asyncio.run(bot._on_message(_message("/watches")))
        asyncio.run(bot._on_message(_message("!frobnicate now")))  # not ours: ignored
        asyncio.run(bot._on_message(_message("hello there")))
        asyncio.run(bot._on_message(_message("!help", bot_author=True)))  # bots ignored
        assert seen == [
            ("123", "rob435", "/add", "lithosale [100]"),
            ("123", "rob435", "/watches", ""),
        ]
        assert [s["content"] for s in channel.sent] == ["ok", "ok"]
    finally:
        asyncio.run(bot.close())


def test_on_message_resolves_user_mentions_to_usernames() -> None:
    bot = _bot()
    _with_fake_channel(bot)
    seen: list[str] = []

    async def handler(channel_id, username, command, arg):
        seen.append(arg)
        return ""

    bot._handler = handler
    alice = SimpleNamespace(id=42, name="alice.w")
    try:
        asyncio.run(bot._on_message(_message("!invite <@42>", mentions=[alice])))
        asyncio.run(bot._on_message(_message("!invite <@!42>", mentions=[alice])))
        asyncio.run(bot._on_message(_message("!invite <@777>", mentions=[alice])))
        assert seen == ["@alice.w", "@alice.w", "<@777>"]
    finally:
        asyncio.run(bot.close())


# ----- button clicks -----------------------------------------------------------------


def test_on_interaction_dispatches_button_clicks_and_toasts_ephemerally() -> None:
    bot = _bot()
    seen: list[tuple] = []

    async def callback(channel_id, username, custom_id, message_id):
        seen.append((channel_id, username, custom_id, message_id))
        return "Muted this listing."

    bot._callback_handler = callback
    interaction = _interaction(
        type=discord.InteractionType.component,
        data={"custom_id": "mv:1:2", "component_type": 2},
        message=SimpleNamespace(id=42),
    )
    try:
        asyncio.run(bot._on_interaction(interaction))
        assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
        assert seen == [("123", "rob435", "mv:1:2", 42)]
        assert interaction.followup.sent == [{"content": "Muted this listing.", "ephemeral": True}]
    finally:
        asyncio.run(bot.close())


def test_on_interaction_with_empty_toast_removes_placeholder() -> None:
    bot = _bot()

    async def callback(channel_id, username, custom_id, message_id):
        return ""

    bot._callback_handler = callback
    interaction = _interaction(
        type=discord.InteractionType.component, data={"custom_id": "uw:9"}, message=None
    )
    try:
        asyncio.run(bot._on_interaction(interaction))
        assert interaction.followup.sent == [] and interaction.deleted == 1
    finally:
        asyncio.run(bot.close())


def test_on_interaction_ignores_slash_commands_they_go_through_the_tree() -> None:
    bot = _bot()
    calls = []

    async def callback(*args):
        calls.append(args)
        return ""

    bot._callback_handler = callback
    interaction = _interaction()  # application_command type
    try:
        asyncio.run(bot._on_interaction(interaction))
        assert calls == [] and interaction.response.deferred == []
    finally:
        asyncio.run(bot.close())


# ----- slash command registration ------------------------------------------------


def test_every_service_command_is_registered_within_discord_limits() -> None:
    bot = _bot()
    try:
        names = {command.name for command in bot.tree.get_commands()}
        expected = {name for name, _, _ in SLASH_COMMANDS} | {name for name, _ in USER_COMMANDS}
        assert names == expected
        for command in bot.tree.get_commands():
            payload = command.to_dict(bot.tree)
            assert 1 <= len(payload["name"]) <= 32 and payload["name"].islower()
            assert 1 <= len(payload["description"]) <= 100
            for option in payload.get("options", []):
                assert 1 <= len(option["description"]) <= 100
        # Free-text commands expose one optional string "args"; invite takes a user.
        add = bot.tree.get_command("add").to_dict(bot.tree)
        assert add["options"] == [
            {"type": 3, "name": "args", "description": "seller [minprice] — e.g. lithosale [100]", "required": False}
        ]
        assert bot.tree.get_command("help").to_dict(bot.tree).get("options", []) == []
        invite = bot.tree.get_command("invite").to_dict(bot.tree)
        assert invite["options"][0]["type"] == 6 and invite["options"][0]["name"] == "user"
        assert {"help", "start", "invite", "watch"} <= TEXT_COMMANDS
    finally:
        asyncio.run(bot.close())


# ----- alert rendering -------------------------------------------------------------


def test_seller_line_builds_marketplace_hyperlink() -> None:
    bot = _bot("EBAY-GB")
    try:
        line = bot._seller_line("colourfu59", 98.6, 1004)
        assert "[colourfu59](https://www.ebay.co.uk/usr/colourfu59)" in line
        assert "⭐ 98.6% (1,004)" in line
        assert "⭐" not in bot._seller_line("seller_x", "", "")
        assert r"[seller\_x](https://www.ebay.co.uk/usr/seller_x)" in bot._seller_line("seller_x", "", "")
    finally:
        asyncio.run(bot.close())


def test_listing_embed_carries_header_title_link_rating_and_dates() -> None:
    bot = _bot("EBAY-GB")
    try:
        embed = bot.listing_embed(
            _listing(), header="🔴 ENDED", colour=0x123456, ended_at="2026-06-14T20:01:00.000Z"
        )
        assert embed.author.name == "🔴 ENDED"
        assert embed.title == "Dyson Airblade HU02" and embed.url == "https://www.ebay.co.uk/itm/1"
        assert embed.colour.value == 0x123456
        assert "💰 £430.00" in embed.description
        assert "[colourfu59](https://www.ebay.co.uk/usr/colourfu59)" in embed.description
        assert "98.6% (1,004)" in embed.description
        assert "🕒 Listed 13 Jun 2026" in embed.description
        assert "🏁 Ended 14 Jun 2026" in embed.description
        assert embed.image.url == "https://i.ebayimg.com/images/g/abc/s-l1600.jpg"
    finally:
        asyncio.run(bot.close())


def test_listing_embed_escapes_markdown_and_clips_description() -> None:
    bot = _bot(send_images=False)
    try:
        embed = bot.listing_embed(
            _listing(description="really *rare* item_" + "x" * 600, category="Hand Dryers"),
            header="🆕 NEW LISTING",
            colour=1,
        )
        assert r"📝 really \*rare\* item\_" in embed.description
        assert "..." in embed.description  # 500-char snippet
        assert "📂 Hand Dryers" in embed.description
        assert embed.image.url is None  # DISCORD_SEND_IMAGES=false
    finally:
        asyncio.run(bot.close())


def _capture_sends(bot: DiscordBot) -> list[dict]:
    captured: list[dict] = []

    async def fake_send(channel_id, *, content=None, embed=None, rows=None):
        captured.append({"channel_id": channel_id, "content": content, "embed": embed, "rows": rows})
        return 1

    bot._send = fake_send  # type: ignore[method-assign]
    return captured


def test_listing_notifications_send_embed_with_link_button() -> None:
    bot = _bot("EBAY-GB")
    captured = _capture_sends(bot)
    try:
        asyncio.run(bot.notify_listing("c", _listing()))
        asyncio.run(bot.notify_quantity_increase("c", _listing(), 2, 5))
        asyncio.run(bot.notify_price_drop("c", _listing(), 430.0, 380.0, 11.6))
        headers = [c["embed"].author.name for c in captured]
        assert headers == ["🆕 NEW LISTING", "📈 QUANTITY INCREASED", "📉 PRICE DROP"]
        assert "**2 → 5 available**" in captured[1]["embed"].description
        assert "**£430.00 → £380.00 (-12%)**" in captured[2]["embed"].description
        for call in captured:
            assert call["channel_id"] == "c"
            assert call["rows"] == [[Button("🔗 View on eBay", url="https://www.ebay.co.uk/itm/1")]]
    finally:
        asyncio.run(bot.close())


def test_deal_embed_and_buttons() -> None:
    bot = _bot("EBAY-GB")
    try:
        item = _item()
        embed = bot.deal_embed(
            item,
            market_price=430.0,
            discount_percent=27.9,
            query="dyson airblade hu02",
            profit=65.0,
            roi=21.0,
            distribution=(380.0, 430.0, 510.0),
            comps=[440.0, 425.0, 450.0],
        )
        assert embed.author.name == "💸 DEAL FOUND" and embed.colour.value == COLOUR_DEAL
        assert embed.title == "Dyson Airblade HU02" and embed.url == item.url
        text = embed.description
        assert "**28% below market on “dyson airblade hu02”**" in text
        assert "💰 £309.99 (incl. £9.99 shipping)" in text
        assert "📊 Market ≈ £430.00 (range £380.00–£510.00)" in text
        assert "✅ You save ≈ £120.01" in text
        assert "💵 Est. flip profit ≈ £65.00 (+21% ROI)" in text
        assert "🧾 Recently sold: £440.00 · £425.00 · £450.00" in text
        assert "💬 Accepts offers" in text
        assert "[bargain22](https://www.ebay.co.uk/usr/bargain22)" in text and "99.2% (540)" in text

        rows = DiscordBot.deal_buttons(item, watch_id=7)
        assert rows[0] == [Button("🔗 Open on eBay", url=item.url)]
        assert [b.custom_id for b in rows[1]] == ["mv:7:1", "bl:7:1"]
        assert [b.custom_id for b in rows[2]] == ["fu:7:1", "fd:7:1"]
        assert DiscordBot.deal_buttons(item, watch_id=None) == [rows[0]]
    finally:
        asyncio.run(bot.close())


def test_deal_embed_header_variants() -> None:
    bot = _bot("EBAY-GB")
    try:
        auction = _item(buying_options=("AUCTION",), bid_count=0, end_date="2026-06-14T20:01:00Z")
        common = dict(market_price=430.0, discount_percent=30, query="q")
        embed = bot.deal_embed(auction, low_competition=True, **common)
        assert embed.author.name == "🔨 AUCTION DEAL" and embed.colour.value == COLOUR_AUCTION
        assert "💰 £309.99 current bid" in embed.description
        assert "⏰ Ends 14 Jun 2026, 20:01 UTC · 0 bid(s)" in embed.description
        assert "🏁 Low competition" in embed.description

        embed = bot.deal_embed(auction, ending_soon=True, **common)
        assert embed.author.name == "⏰ AUCTION ENDING SOON"
        assert embed.colour.value == COLOUR_ENDING_SOON

        embed = bot.deal_embed(_item(), offer_estimate=350.0, **common)
        assert embed.author.name == "🤝 DEAL IF YOU OFFER" and embed.colour.value == COLOUR_OFFER
        assert "💰 Listed at £309.99" in embed.description
        assert "🤝 Deal if you offer ≈ £350.00 (save ≈ £80.00)" in embed.description
        assert "Accepts offers" not in embed.description  # implied by the offer framing

        embed = bot.deal_embed(_item(total_price=600.0), lot_quantity=3, **dict(common, market_price=900.0))
        assert embed.author.name == "📦 LOT DEAL" and embed.colour.value == COLOUR_LOT
        assert "💰 £600.00 for 3 units (≈ £200.00/unit)" in embed.description
        assert "📊 Lot worth ≈ £900.00 (£300.00/unit market)" in embed.description
    finally:
        asyncio.run(bot.close())


def test_notify_deal_sends_embed_and_buttons_to_channel() -> None:
    bot = _bot("EBAY-GB")
    captured = _capture_sends(bot)
    try:
        asyncio.run(
            bot.notify_deal(
                "c", _item(), market_price=430.0, discount_percent=27.9, query="q", watch_id=3
            )
        )
        assert captured[0]["embed"].author.name == "💸 DEAL FOUND"
        assert [b.custom_id for b in captured[0]["rows"][1]] == ["mv:3:1", "bl:3:1"]
    finally:
        asyncio.run(bot.close())


def test_arbitrage_embed() -> None:
    bot = _bot()
    try:
        embed = bot.arbitrage_embed(
            "lego 75192",
            {
                "gap_percent": 24.4,
                "home_currency": "GBP",
                "buy_marketplace": "EBAY_DE",
                "buy_price": 500.0,
                "buy_currency": "EUR",
                "buy_home": 430.0,
                "sell_marketplace": "EBAY_GB",
                "sell_price": 535.0,
                "sell_currency": "GBP",
                "sell_home": 535.0,
            },
        )
        assert embed.title == "🌍 ARBITRAGE OPPORTUNITY"
        assert "**24% gap on “lego 75192”**" in embed.description
        assert "🟢 Buy in DE: €500.00 (≈ £430.00)" in embed.description
        assert "🔴 Sell in GB: £535.00 (≈ £535.00)" in embed.description
    finally:
        asyncio.run(bot.close())
