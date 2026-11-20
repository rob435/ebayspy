"""Discord transport for ebayspy: alert embeds, slash + text commands, buttons.

Two halves, deliberately decoupled:

* **Alerts** go out over Discord's REST API after a static token login. That
  needs no gateway connection, so the one-shot ``ebayspy check`` and the
  wake-poll agent can deliver alerts exactly as the long-running tracker does.
* **Controls** — slash commands, ``!text`` commands and button clicks — arrive
  over the gateway, which :meth:`DiscordBot.run` keeps connected for the
  tracker. Losing the gateway never blocks an alert.

Button clicks are handled from the raw ``on_interaction`` event keyed on the
button's ``custom_id`` (``mv:<watch>:<item>`` etc.), so a button on a message
sent before a restart still works — no persistent-view bookkeeping needed.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands

from .pricing import FairValue
from .formatting import (
    evidence_line,
    EBAY_DOMAINS,
    format_listing_type,
    format_money,
    format_price,
    format_seller_rating,
    format_timestamp,
    spec_line,
    truncate_text,
)
from .models import Listing, MarketItem

log = logging.getLogger(__name__)

CommandHandler = Callable[[str, str | None, str, str], Awaitable[str]]
# (channel_id, username, custom_id, message_id) -> toast text. message_id lets a
# handler edit the message the button lives on (e.g. refresh a list after a click).
CallbackHandlerType = Callable[[str, str | None, str, int | None], Awaitable[str]]

# Discord hard limits.
CONTENT_LIMIT = 2000
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_TITLE_LIMIT = 256
BUTTON_LABEL_LIMIT = 80
CUSTOM_ID_LIMIT = 100
BUTTONS_PER_ROW = 5
MAX_BUTTON_ROWS = 5
MAX_BUTTONS = BUTTONS_PER_ROW * MAX_BUTTON_ROWS
SLASH_DESCRIPTION_LIMIT = 100

# Embed accent colours per event type.
COLOUR_NEW = 0x2ECC71
COLOUR_ENDED = 0xE74C3C
COLOUR_RESTOCK = 0x3498DB
COLOUR_PRICE_DROP = 0xE67E22
COLOUR_DEAL = 0xF1C40F
COLOUR_AUCTION = 0x9B59B6
COLOUR_ENDING_SOON = 0xC0392B
COLOUR_OFFER = 0x1ABC9C
COLOUR_LOT = 0x8E44AD
COLOUR_ARBITRAGE = 0x16A085


class DiscordError(RuntimeError):
    """A Discord API failure, surfaced as a plain message for the service's logs."""


@dataclass(frozen=True)
class Button:
    """A platform-neutral button the service can attach to a message.

    Exactly one of ``custom_id`` (delivered to the callback handler on click)
    or ``url`` (opens a link) should be set.
    """

    label: str
    custom_id: str | None = None
    url: str | None = None
    style: str = "secondary"  # primary · secondary · success · danger


ButtonRows = list[list[Button]]

_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}

# Slash commands: (name, description, hint for the free-text ``args`` option;
# None = the command takes no arguments). The service parses ``args`` exactly
# as it parsed the text after a Telegram command, so ``/watch`` with
# ``args: dyson airblade hu02 under:400`` behaves as before.
SLASH_COMMANDS: tuple[tuple[str, str, str | None], ...] = (
    ("help", "What ebayspy can do, with examples", None),
    (
        "add",
        "Watch a seller: alerts on new, price drop, ended/sold, restock",
        "seller [minprice] — e.g. lithosale [100]",
    ),
    ("remove", "Stop watching a seller", "seller"),
    ("list", "Watched sellers, with a 🗑 button per row", None),
    ("floor", "Set or clear a seller's price floor", "seller [100]  ·  seller none"),
    ("status", "Last check result per seller", None),
    ("health", "Liveness: uptime, last poll, counts, errors", None),
    ("check", "Poll the watch list right now", None),
    (
        "observe",
        "Fast new-listing alerts for a seller, on its own interval",
        "seller [interval] [minprice] — e.g. acme 90s [100]",
    ),
    ("unobserve", "Stop observing a seller", "seller"),
    ("observing", "Observed sellers, intervals and floors", None),
    ("interval", "Change a seller's observe interval", "seller <time> — e.g. acme 5m"),
    (
        "watch",
        "Alert when an item lists below the going market rate",
        "terms [condition:new|used] [under:PRICE] [discount:%] [every:TIME] …",
    ),
    ("unwatch", "Stop a market watch", "<id>  (ids from /watches)"),
    ("watches", "Market watches with market estimate, variant and trend", None),
    ("quote", "Fair value, confidence and evidence for a watch", "<id>"),
    ("demand", "Demand / liquidity read for a watch", "<id>"),
    ("refimage", "Pin, show or clear a watch's reference photo", "<id> [image-url | clear]"),
    ("invites", "Admins and invited users", None),
)
# Commands that take a Discord user (a picker in the slash UI) instead of text.
USER_COMMANDS: tuple[tuple[str, str], ...] = (
    ("invite", "Let a Discord user control the bot"),
    ("uninvite", "Revoke a user's invite"),
)
# Names answered when typed as ``!name`` / ``/name`` text. Anything else is
# ignored so the bot never replies "Unknown command" to other bots' prefixes.
TEXT_COMMANDS = frozenset(name for name, _, _ in SLASH_COMMANDS) | frozenset(
    name for name, _ in USER_COMMANDS
) | {"start"}

_MD_CHARS = re.compile(r"([\\*_~`|])")
_MD_LINE_START = re.compile(r"^(\s*)([#>-])", re.MULTILINE)
_URL = re.compile(r"https?://\S+")
_MENTION = re.compile(r"<@!?(\d+)>")


def escape_md(text: str) -> str:
    """Escape Discord markdown so seller names like ``seller_one`` render verbatim.

    URLs are left untouched (escaping their underscores would break them), and a
    line-leading ``#``, ``>`` or ``-`` is escaped in place so an indented list
    line never gains a stray backslash before its whitespace.
    """

    def escape_chars(segment: str) -> str:
        return _MD_CHARS.sub(r"\\\1", segment)

    out: list[str] = []
    pos = 0
    for match in _URL.finditer(text):
        out.append(escape_chars(text[pos : match.start()]))
        out.append(match.group(0))
        pos = match.end()
    out.append(escape_chars(text[pos:]))
    return _MD_LINE_START.sub(r"\1\\\2", "".join(out))


def _link(text: str, url: str) -> str:
    """A masked link; parentheses in the URL would end the markdown early."""
    return f"[{text}]({url.replace('(', '%28').replace(')', '%29')})"


def _currency_of(price: str | None) -> str:
    tokens = (price or "").split()
    last = tokens[-1] if len(tokens) >= 2 else ""
    return last if len(last) == 3 and last.isalpha() else ""


@dataclass
class _ReplyTarget:
    """The slash-command interaction a service reply should answer.

    Set in a context variable while a command handler runs, so a message the
    service sends to the invoking channel becomes the interaction's response
    (and appears "in reply to" the command) rather than a loose channel post.
    """

    interaction: discord.Interaction
    used: bool = False


_reply_target: contextvars.ContextVar[_ReplyTarget | None] = contextvars.ContextVar(
    "ebayspy_discord_reply", default=None
)


class DiscordBot:
    def __init__(
        self,
        token: str,
        ebay_global_id: str = "EBAY-US",
        send_images: bool = True,
        message_content_intent: bool = False,
    ) -> None:
        self.token = token
        self.ebay_domain = EBAY_DOMAINS.get(ebay_global_id.upper(), "ebay.com")
        self.send_images = send_images
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.dm_messages = True
        # Privileged: needed only for ``!text`` commands in server channels.
        # DMs to the bot always carry their content.
        intents.message_content = message_content_intent
        self.client = discord.Client(intents=intents)
        self.tree = app_commands.CommandTree(self.client)
        self._handler: CommandHandler | None = None
        self._callback_handler: CallbackHandlerType | None = None
        self._logged_in = False
        self._login_lock = asyncio.Lock()
        self._synced = False
        self._channels: dict[int, discord.abc.Messageable] = {}
        self._register_commands()
        self._register_events()

    # ----- lifecycle -----------------------------------------------------------

    async def _ensure_login(self) -> None:
        """Static (REST-only) login; enough to send alerts without a gateway."""
        if self._logged_in:
            return
        async with self._login_lock:
            if self._logged_in:
                return
            try:
                await self.client.login(self.token)
            except discord.LoginFailure:
                raise DiscordError(
                    "Discord rejected DISCORD_BOT_TOKEN — copy the bot token again from "
                    "the Developer Portal (Bot → Reset Token)."
                ) from None
            except (discord.HTTPException, aiohttp.ClientError, OSError) as exc:
                raise DiscordError(f"Could not log in to Discord: {exc}") from None
            self._logged_in = True

    async def run(self, handler: CommandHandler, callback_handler: CallbackHandlerType) -> None:
        """Keep the gateway connected so commands and button clicks arrive.

        Runs until cancelled. Alerts never depend on this: they use REST only.
        """
        self._handler = handler
        self._callback_handler = callback_handler
        while True:
            try:
                await self._ensure_login()
                await self.client.connect(reconnect=True)
                return  # connect() only returns once the client has been closed
            except asyncio.CancelledError:
                raise
            except discord.PrivilegedIntentsRequired:
                log.error(
                    "Discord refused the Message Content intent. Enable it under Bot → "
                    "Privileged Gateway Intents in the Developer Portal, or set "
                    "DISCORD_MESSAGE_CONTENT_INTENT=false. Commands are offline until "
                    "then; alerts still send."
                )
                return
            except DiscordError as exc:
                log.error("%s — commands offline, retrying in 60s", exc)
                await asyncio.sleep(60)
            except Exception:
                log.exception("Discord gateway failed; reconnecting in 10s")
                await asyncio.sleep(10)

    async def close(self) -> None:
        try:
            await self.client.close()
        except Exception:
            log.debug("Error closing Discord client", exc_info=True)

    # ----- command registration ----------------------------------------------

    def _register_commands(self) -> None:
        contexts = app_commands.AppCommandContext(guild=True, dm_channel=True)
        installs = app_commands.AppInstallationType(guild=True)
        for name, description, hint in SLASH_COMMANDS:
            callback = self._slash_callback(name, with_args=hint is not None)
            if hint is not None:
                callback = app_commands.describe(args=hint[:SLASH_DESCRIPTION_LIMIT])(callback)
            self.tree.add_command(
                app_commands.Command(
                    name=name,
                    description=description[:SLASH_DESCRIPTION_LIMIT],
                    callback=callback,
                    allowed_contexts=contexts,
                    allowed_installs=installs,
                )
            )
        for name, description in USER_COMMANDS:
            self.tree.add_command(
                app_commands.Command(
                    name=name,
                    description=description[:SLASH_DESCRIPTION_LIMIT],
                    callback=self._user_callback(name),
                    allowed_contexts=contexts,
                    allowed_installs=installs,
                )
            )

    def _slash_callback(self, name: str, *, with_args: bool):
        if with_args:

            async def callback(interaction: discord.Interaction, args: str = "") -> None:
                await self._dispatch_slash(interaction, name, args)

        else:

            async def callback(interaction: discord.Interaction) -> None:  # type: ignore[misc]
                await self._dispatch_slash(interaction, name, "")

        return callback

    def _user_callback(self, name: str):
        async def callback(interaction: discord.Interaction, user: discord.User) -> None:
            await self._dispatch_slash(interaction, name, f"@{user.name}")

        return callback

    def _register_events(self) -> None:
        @self.client.event
        async def on_ready() -> None:
            await self._on_ready()

        @self.client.event
        async def on_message(message: discord.Message) -> None:
            await self._on_message(message)

        @self.client.event
        async def on_interaction(interaction: discord.Interaction) -> None:
            await self._on_interaction(interaction)

    async def _on_ready(self) -> None:
        log.info("Discord gateway ready as %s", self.client.user)
        if self._synced:
            return
        try:
            synced = await self.tree.sync()
        except Exception:
            log.exception("Could not sync Discord slash commands; will retry on next connect")
            return
        self._synced = True
        log.info(
            "Synced %d slash commands (global; new commands can take a few minutes to "
            "show — text commands like !help work immediately)",
            len(synced),
        )

    # ----- inbound: commands ---------------------------------------------------

    async def _dispatch_slash(self, interaction: discord.Interaction, name: str, arg: str) -> None:
        if self._handler is None:
            await self._safe_respond(interaction, "ebayspy is still starting up — try again shortly.")
            return
        try:
            # Handlers may call eBay (e.g. /watch prices the market) and exceed
            # Discord's 3-second window, so acknowledge first.
            await interaction.response.defer(thinking=True)
        except discord.HTTPException:
            log.debug("Could not defer interaction", exc_info=True)
            return
        target = _ReplyTarget(interaction)
        token = _reply_target.set(target)
        try:
            reply = await self._handler(
                str(interaction.channel_id), self._username(interaction), f"/{name}", arg.strip()
            )
        except Exception:
            log.exception("Command /%s failed", name)
            reply = "Something went wrong handling that command."
        finally:
            _reply_target.reset(token)
        try:
            if reply:
                content, embed = self._text_payload(reply)
                await interaction.followup.send(**self._payload(content, embed))
            elif not target.used:
                # The handler sent its own message (a list with buttons) or had
                # nothing to say; drop the "thinking…" placeholder.
                await interaction.delete_original_response()
        except discord.HTTPException:
            log.debug("Could not finish interaction response", exc_info=True)

    async def _on_message(self, message: discord.Message) -> None:
        """``!add seller`` / ``/add seller`` typed as plain text."""
        if self._handler is None or message.author.bot:
            return
        content = (message.content or "").strip()
        if len(content) < 2 or content[0] not in "/!":
            return
        name, _, arg = content[1:].partition(" ")
        name = name.lower()
        if name not in TEXT_COMMANDS:
            return
        arg = self._resolve_mentions(arg, message.mentions)
        channel_id = str(message.channel.id)
        try:
            reply = await self._handler(channel_id, message.author.name, f"/{name}", arg.strip())
        except Exception:
            log.exception("Command /%s failed", name)
            reply = "Something went wrong handling that command."
        if reply:
            try:
                await self.send_message(channel_id, reply)
            except DiscordError:
                log.warning("Could not reply in Discord channel %s", channel_id, exc_info=True)

    @staticmethod
    def _resolve_mentions(text: str, users: Iterable[discord.abc.User]) -> str:
        """Turn ``<@123>`` mentions into ``@username`` so /invite @user works."""
        by_id = {str(user.id): user.name for user in users}

        def replace(match: re.Match[str]) -> str:
            name = by_id.get(match.group(1))
            return f"@{name}" if name else match.group(0)

        return _MENTION.sub(replace, text)

    @staticmethod
    def _username(interaction: discord.Interaction) -> str | None:
        return interaction.user.name if interaction.user else None

    # ----- inbound: button clicks ---------------------------------------------

    async def _on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return  # slash commands are dispatched through the command tree
        data: dict[str, Any] = interaction.data or {}  # type: ignore[assignment]
        custom_id = str(data.get("custom_id") or "")
        if not custom_id or self._callback_handler is None:
            await self._safe_ack(interaction)
            return
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.HTTPException:
            log.debug("Could not defer component interaction", exc_info=True)
            return
        message_id = interaction.message.id if interaction.message else None
        try:
            toast = await self._callback_handler(
                str(interaction.channel_id), self._username(interaction), custom_id, message_id
            )
        except Exception:
            log.exception("Button %s failed", custom_id)
            toast = "Something went wrong."
        try:
            if toast:
                # Ephemeral: only the clicker sees it, like Telegram's toast.
                await interaction.followup.send(toast, ephemeral=True)
            else:
                await interaction.delete_original_response()
        except discord.HTTPException:
            log.debug("Could not answer button click", exc_info=True)

    @staticmethod
    async def _safe_ack(interaction: discord.Interaction) -> None:
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass

    @staticmethod
    async def _safe_respond(interaction: discord.Interaction, text: str) -> None:
        try:
            await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    # ----- outbound ----------------------------------------------------------

    async def _channel(self, channel_id: str) -> discord.abc.Messageable:
        try:
            cid = int(str(channel_id).strip())
        except ValueError:
            raise DiscordError(f"{channel_id!r} is not a Discord channel id") from None
        cached = self._channels.get(cid)
        if cached is not None:
            return cached
        await self._ensure_login()
        channel = self.client.get_channel(cid)
        if channel is None:
            try:
                channel = await self.client.fetch_channel(cid)
            except discord.NotFound:
                raise DiscordError(
                    f"Discord channel {cid} not found — deleted, or the bot isn't in that server"
                ) from None
            except discord.Forbidden:
                raise DiscordError(f"The bot can't see Discord channel {cid}") from None
            except (discord.HTTPException, aiohttp.ClientError, OSError) as exc:
                raise DiscordError(f"Could not fetch Discord channel {cid}: {exc}") from None
        if not isinstance(channel, discord.abc.Messageable):
            raise DiscordError(f"Discord channel {cid} can't receive messages")
        self._channels[cid] = channel
        return channel

    @staticmethod
    def build_view(rows: ButtonRows) -> discord.ui.View:
        """Lay buttons out in Discord's 5×5 grid, clipping labels and ids to the caps."""
        view = discord.ui.View(timeout=None)
        for row_index, row in enumerate(rows[:MAX_BUTTON_ROWS]):
            for button in row[:BUTTONS_PER_ROW]:
                label = button.label[:BUTTON_LABEL_LIMIT]
                if button.url:
                    view.add_item(discord.ui.Button(label=label, url=button.url, row=row_index))
                else:
                    view.add_item(
                        discord.ui.Button(
                            label=label,
                            custom_id=(button.custom_id or "")[:CUSTOM_ID_LIMIT],
                            style=_STYLES.get(button.style, discord.ButtonStyle.secondary),
                            row=row_index,
                        )
                    )
        return view

    @staticmethod
    def _text_payload(text: str) -> tuple[str | None, discord.Embed | None]:
        """Plain content when it fits Discord's 2000-char cap, else an embed (4096)."""
        escaped = escape_md(text)
        if len(escaped) <= CONTENT_LIMIT:
            return escaped, None
        return None, discord.Embed(description=truncate_text(escaped, EMBED_DESCRIPTION_LIMIT))

    @staticmethod
    def _payload(
        content: str | None, embed: discord.Embed | None, view: discord.ui.View | None = None
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        return kwargs

    async def _send(
        self,
        channel_id: str,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        rows: ButtonRows | None = None,
    ) -> int:
        """Send to a channel — or, while answering a slash command in that
        channel, as the command's response. Returns the message id."""
        view = self.build_view(rows) if rows else None
        kwargs = self._payload(content, embed, view)
        target = _reply_target.get()
        try:
            if (
                target is not None
                and not target.used
                and str(target.interaction.channel_id) == str(channel_id)
            ):
                target.used = True
                message = await target.interaction.followup.send(wait=True, **kwargs)
            else:
                channel = await self._channel(channel_id)
                message = await channel.send(**kwargs)
        except (discord.HTTPException, aiohttp.ClientError, OSError) as exc:
            raise DiscordError(f"Discord rejected the message: {exc}") from None
        finally:
            if view is not None:
                # Clicks are handled from on_interaction by custom_id; don't keep
                # a live View per alert around.
                view.stop()
        return message.id

    async def send_message(self, channel_id: str, text: str, buttons: ButtonRows | None = None) -> int:
        """Send plain text (markdown-escaped) with optional buttons."""
        content, embed = self._text_payload(text)
        return await self._send(channel_id, content=content, embed=embed, rows=buttons)

    async def edit_message(
        self, channel_id: str, message_id: int, text: str, buttons: ButtonRows | None = None
    ) -> None:
        """Replace a message's text (and buttons) in place — used to refresh a
        clickable list after a row is removed. ``buttons=[]`` clears them;
        ``None`` leaves them untouched."""
        content, embed = self._text_payload(text)
        channel = await self._channel(channel_id)
        kwargs: dict[str, Any] = {"content": content, "embed": embed}
        view = None
        if buttons is not None:
            view = self.build_view(buttons)
            kwargs["view"] = view
        try:
            get_partial = getattr(channel, "get_partial_message", None)
            if get_partial is not None:
                await get_partial(int(message_id)).edit(**kwargs)
            else:  # pragma: no cover - every messageable channel type has it
                message = await channel.fetch_message(int(message_id))  # type: ignore[attr-defined]
                await message.edit(**kwargs)
        except (discord.HTTPException, aiohttp.ClientError, OSError) as exc:
            raise DiscordError(f"Discord rejected the edit: {exc}") from None
        finally:
            if view is not None:
                view.stop()

    # ----- alert rendering ------------------------------------------------------

    def _seller_line(self, seller: str, percent: object, score: object) -> str:
        name = escape_md(seller) if seller else "Unknown"
        if seller:
            name = _link(name, f"https://www.{self.ebay_domain}/usr/{quote(seller, safe='')}")
        rating = format_seller_rating(percent, score)
        return f"👤 {name} · ⭐ {rating}" if rating else f"👤 {name}"

    def _image(self, embed: discord.Embed, image_url: str | None) -> None:
        if self.send_images and image_url and image_url.startswith(("http://", "https://")):
            embed.set_image(url=image_url)

    @staticmethod
    def _link_buttons(label: str, url: str) -> ButtonRows:
        # A link button needs a real URL or Discord rejects the whole message.
        return [[Button(label, url=url)]] if url.startswith(("http://", "https://")) else []

    def listing_embed(
        self,
        listing: Listing,
        *,
        header: str,
        colour: int,
        subheader: str | None = None,
        ended_at: str | None = None,
    ) -> discord.Embed:
        # Embed titles and author names are plain text (no markdown), so the
        # listing title goes there unescaped and clickable.
        embed = discord.Embed(colour=colour, title=truncate_text(listing.title, EMBED_TITLE_LIMIT))
        if listing.url.startswith(("http://", "https://")):
            embed.url = listing.url
        embed.set_author(name=header)
        lines: list[str] = []
        if subheader:
            lines.extend([f"**{escape_md(subheader)}**", ""])
        lines.append(f"💰 {escape_md(format_price(listing.price))}")
        lines.append(
            self._seller_line(
                listing.seller, listing.seller_feedback_percent, listing.seller_feedback_score
            )
        )
        listing_type = format_listing_type(listing.listing_type)
        if listing_type:
            lines.append(f"🏷️ {escape_md(listing_type)}")
        if listing.quantity_available:
            lines.append(f"📦 {escape_md(listing.quantity_available)} available")
        if listing.category:
            lines.append(f"📂 {escape_md(listing.category)}")
        listed = format_timestamp(listing.listed_at)
        if listed:
            lines.append(f"🕒 Listed {listed}")
        ended = format_timestamp(ended_at)
        if ended:
            lines.append(f"🏁 Ended {ended}")
        description = listing.description.strip()
        if description:
            if len(description) > 500:
                description = description[:497].rstrip() + "..."
            lines.extend(["", f"📝 {escape_md(description)}"])
        embed.description = truncate_text("\n".join(lines), EMBED_DESCRIPTION_LIMIT)
        self._image(embed, listing.image_url)
        return embed

    async def _notify_listing_event(
        self,
        channel_id: str,
        listing: Listing,
        *,
        header: str,
        colour: int,
        subheader: str | None = None,
        ended_at: str | None = None,
    ) -> None:
        embed = self.listing_embed(
            listing, header=header, colour=colour, subheader=subheader, ended_at=ended_at
        )
        await self._send(
            channel_id, embed=embed, rows=self._link_buttons("🔗 View on eBay", listing.url)
        )

    async def notify_listing(self, channel_id: str, listing: Listing) -> None:
        await self._notify_listing_event(
            channel_id, listing, header="🆕 NEW LISTING", colour=COLOUR_NEW
        )

    async def notify_ended_listing(
        self, channel_id: str, listing: Listing, ended_at: str | None = None
    ) -> None:
        await self._notify_listing_event(
            channel_id, listing, header="🔴 ENDED", colour=COLOUR_ENDED, ended_at=ended_at
        )

    async def notify_quantity_increase(
        self, channel_id: str, listing: Listing, previous_quantity: int, current_quantity: int
    ) -> None:
        await self._notify_listing_event(
            channel_id,
            listing,
            header="📈 QUANTITY INCREASED",
            colour=COLOUR_RESTOCK,
            subheader=f"{previous_quantity} → {current_quantity} available",
        )

    async def notify_price_drop(
        self, channel_id: str, listing: Listing, old_price: float, new_price: float, pct: float
    ) -> None:
        # Derive the currency from the listing's own price string so old → new
        # render in the right units (the amounts arrive as bare floats).
        cur = _currency_of(listing.price)
        await self._notify_listing_event(
            channel_id,
            listing,
            header="📉 PRICE DROP",
            colour=COLOUR_PRICE_DROP,
            subheader=f"{format_money(old_price, cur)} → {format_money(new_price, cur)} (-{pct:.0f}%)",
        )

    def deal_embed(
        self,
        item: MarketItem,
        *,
        market_price: float,
        discount_percent: float,
        query: str,
        variant: str | None = None,
        profit: float | None = None,
        roi: float | None = None,
        ending_soon: bool = False,
        trend: str = "",
        demand: str = "",
        distribution: tuple[float, float, float] | None = None,
        comps: list[float] | None = None,
        low_competition: bool = False,
        risk: str = "",
        lot_quantity: int | None = None,
        vision: str = "",
        offer_estimate: float | None = None,
        quote: FairValue | None = None,
        quick_profit: float | None = None,
    ) -> discord.Embed:
        saving = market_price - item.total_price
        cur = item.currency
        is_offer = offer_estimate is not None and not lot_quantity
        if lot_quantity:
            per_unit = item.total_price / lot_quantity
            unit_market = market_price / lot_quantity
            price_line = (
                f"💰 {format_money(item.total_price, cur)} for {lot_quantity} units"
                f" (≈ {format_money(per_unit, cur)}/unit)"
            )
            market_label = (
                f"📊 Lot worth ≈ {format_money(market_price, cur)}"
                f" ({format_money(unit_market, cur)}/unit market)"
            )
            header, colour = "📦 LOT DEAL", COLOUR_LOT
        else:
            bid_note = " current bid" if item.is_auction else ""
            price_line = f"💰 {format_money(item.total_price, cur)}{bid_note}"
            if item.shipping_cost:
                price_line += f" (incl. {format_money(item.shipping_cost, cur)} shipping)"
            # When the watch spans variants, the market figure is for this item's
            # own variant (e.g. 256GB), so the comparison is always like-for-like.
            market_label = f"📊 Market ≈ {format_money(market_price, cur)}"
            if variant:
                market_label += f" for {variant}"
            if quote is not None:
                # The interval is our uncertainty about fair value, not the
                # spread of listings — a wide one means "treat this with care".
                market_label += (
                    f" (likely {format_money(quote.low, cur)}–{format_money(quote.high, cur)}"
                    f", {quote.confidence}% confidence)"
                )
            elif distribution:
                low, _mid, high = distribution
                market_label += f" (range {format_money(low, cur)}–{format_money(high, cur)})"
            if is_offer:
                header, colour = "🤝 DEAL IF YOU OFFER", COLOUR_OFFER
                price_line = f"💰 Listed at {format_money(item.total_price, cur)}"
            elif ending_soon:
                header, colour = "⏰ AUCTION ENDING SOON", COLOUR_ENDING_SOON
            elif item.is_auction:
                header, colour = "🔨 AUCTION DEAL", COLOUR_AUCTION
            else:
                header, colour = "💸 DEAL FOUND", COLOUR_DEAL
        if is_offer:
            # Never imply the LIST price is the deal — the saving is conditional
            # on a successful Best Offer at the estimated price.
            save_line = (
                f"🤝 Deal if you offer ≈ {format_money(offer_estimate, cur)}"
                f" (save ≈ {format_money(market_price - offer_estimate, cur)})"
            )
        else:
            save_line = f"✅ You save ≈ {format_money(saving, cur)}"
        subheader = (
            f"{discount_percent:.0f}% below market if you offer on “{query}”"
            if is_offer
            else f"{discount_percent:.0f}% below market on “{query}”"
        )
        lines = [
            f"**{escape_md(subheader)}**",
            "",
            escape_md(price_line),
            escape_md(market_label),
            escape_md(save_line),
        ]
        evidence = evidence_line(quote, cur) if not lot_quantity else ""
        if evidence:
            lines.append(escape_md(f"🧮 {evidence}"))
        if profit is not None:
            roi_text = f" ({roi:+.0f}% ROI)" if roi is not None else ""
            quick_text = (
                f" · {format_money(quick_profit, cur)} if sold fast"
                if quick_profit is not None
                else ""
            )
            lines.append(
                escape_md(f"💵 Est. flip profit ≈ {format_money(profit, cur)}{roi_text}{quick_text}")
            )
        if comps:
            joined = " · ".join(format_money(price, cur) for price in comps)
            lines.append(escape_md(f"🧾 Recently sold: {joined}"))
        if trend:
            lines.append(escape_md(trend))
        if demand:
            lines.append(escape_md(demand))
        if item.accepts_offers and not is_offer:
            lines.append("💬 Accepts offers — you may get it lower")
        if item.is_auction:
            ends = format_timestamp(item.end_date)
            bids = f"{item.bid_count} bid(s)" if item.bid_count is not None else "no bids yet"
            lines.append(f"⏰ Ends {ends} · {bids}" if ends else f"⏰ {bids}")
            if low_competition:
                lines.append("🏁 Low competition — win it uncontested")
        if item.condition:
            lines.append(f"🏷️ {escape_md(item.condition)}")
        if vision:
            lines.append(escape_md(vision))
        specs = spec_line(item.aspects)
        if specs:
            lines.append(f"📋 {escape_md(specs)}")
        lines.append(
            self._seller_line(item.seller, item.seller_feedback_percent, item.seller_feedback_score)
        )
        listed = format_timestamp(item.listed_at)
        if listed:
            lines.append(f"🕒 Listed {listed}")
        if risk:
            lines.append(escape_md(risk))
        embed = discord.Embed(colour=colour, title=truncate_text(item.title, EMBED_TITLE_LIMIT))
        if item.url.startswith(("http://", "https://")):
            embed.url = item.url
        embed.set_author(name=header)
        embed.description = truncate_text("\n".join(lines), EMBED_DESCRIPTION_LIMIT)
        self._image(embed, item.image_url)
        return embed

    @staticmethod
    def deal_buttons(item: MarketItem, watch_id: int | None) -> ButtonRows:
        """Buttons under a deal: open the listing, mute the variant, flag it as
        the wrong item (which teaches the watch to exclude it), or rate 👍/👎
        (which tunes the watch's required discount over time)."""
        rows: ButtonRows = []
        if item.url.startswith(("http://", "https://")):
            rows.append([Button("🔗 Open on eBay", url=item.url)])
        if watch_id is not None:
            rows.append(
                [
                    Button("🙈 Mute variant", f"mv:{watch_id}:{item.item_id}"),
                    Button("🚫 Not the item", f"bl:{watch_id}:{item.item_id}", style="danger"),
                ]
            )
            rows.append(
                [
                    Button("👍", f"fu:{watch_id}:{item.item_id}", style="success"),
                    Button("👎", f"fd:{watch_id}:{item.item_id}"),
                ]
            )
        return rows

    async def notify_deal(
        self,
        channel_id: str,
        item: MarketItem,
        *,
        watch_id: int | None = None,
        **kwargs: Any,
    ) -> None:
        embed = self.deal_embed(item, **kwargs)
        await self._send(channel_id, embed=embed, rows=self.deal_buttons(item, watch_id))

    def arbitrage_embed(self, query: str, result: dict) -> discord.Embed:
        home = result["home_currency"]

        def place(marketplace: str) -> str:
            return marketplace.replace("EBAY_", "")

        gap = f"{result['gap_percent']:.0f}% gap on “{query}”"
        lines = [
            f"**{escape_md(gap)}**",
            "",
            escape_md(
                f"🟢 Buy in {place(result['buy_marketplace'])}: "
                f"{format_money(result['buy_price'], result['buy_currency'])} "
                f"(≈ {format_money(result['buy_home'], home)})"
            ),
            escape_md(
                f"🔴 Sell in {place(result['sell_marketplace'])}: "
                f"{format_money(result['sell_price'], result['sell_currency'])} "
                f"(≈ {format_money(result['sell_home'], home)})"
            ),
            "",
            "⚠️ Before fees, shipping, import duty & FX — sanity-check the net.",
        ]
        embed = discord.Embed(colour=COLOUR_ARBITRAGE, title="🌍 ARBITRAGE OPPORTUNITY")
        embed.description = truncate_text("\n".join(lines), EMBED_DESCRIPTION_LIMIT)
        return embed

    async def notify_arbitrage(self, channel_id: str, query: str, result: dict) -> None:
        await self._send(channel_id, embed=self.arbitrage_embed(query, result))
