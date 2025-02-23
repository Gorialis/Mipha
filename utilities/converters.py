"""
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""

import datetime
import logging
import re
import zoneinfo
from typing import Any, Literal, Sequence, Type, TypedDict

import yarl
from discord import app_commands
from discord.ext import commands
from typing_extensions import NotRequired, Self

from utilities.context import Context, GuildContext, Interaction


MYSTBIN_REGEX = re.compile(r"(?:(?:https?://)?(?:beta\.)?(?:mystb\.in\/))?(?P<id>(?:[A-Z]{1}[a-z]+)*)(?P<ext>\.\w+)?")
LOGGER = logging.getLogger(__name__)

__all__ = (
    "RedditMediaURL",
    "WhenAndWhatConverter",
    "DatetimeConverter",
    "WhenAndWhatTransformer",
    "DatetimeTransformer",
    "BadDatetimeTransform",
    "MystbinPasteConverter",
)


class DucklingNormalised(TypedDict):
    unit: Literal["second"]
    value: int


class DucklingResponseValue(TypedDict):
    normalized: DucklingNormalised
    type: Literal["value"]
    unit: str
    value: NotRequired[str]
    minute: NotRequired[int]
    hour: NotRequired[int]
    second: NotRequired[int]
    day: NotRequired[int]
    week: NotRequired[int]
    hour: NotRequired[int]


class DucklingResponse(TypedDict):
    body: str
    dim: Literal["duration", "time"]
    end: int
    start: int
    latent: bool
    value: DucklingResponseValue


class MemeDict(dict):
    def __getitem__(self, k: Sequence[Any]) -> Any:
        for key in self:
            if k in key:
                return super().__getitem__(key)
        raise KeyError(k)


class RedditMediaURL:
    VALID_PATH = re.compile(r"/r/[A-Za-z0-9_]+/comments/[A-Za-z0-9]+(?:/.+)?")

    def __init__(self, url: yarl.URL) -> None:
        self.url = url
        self.filename = url.parts[1] + ".mp4"

    @classmethod
    async def convert(cls: Type[Self], ctx: Context, argument: str) -> Self:
        try:
            url = yarl.URL(argument)
        except Exception:
            raise commands.BadArgument("Not a valid URL.")

        headers = {"User-Agent": "Discord:mipha:v1.0 (by /u/AbstractUmbra)"}
        await ctx.typing()
        if url.host == "v.redd.it":
            # have to do a request to fetch the 'main' URL.
            async with ctx.session.get(url, headers=headers) as resp:
                url = resp.url

        if url.host is None:
            raise commands.BadArgument("Not a valid v.reddit url.")

        is_valid_path = url.host.endswith(".reddit.com") and cls.VALID_PATH.match(url.path)
        if not is_valid_path:
            raise commands.BadArgument("Not a reddit URL.")

        # Now we go the long way
        async with ctx.session.get(url / ".json", headers=headers) as resp:
            if resp.status != 200:
                raise commands.BadArgument(f"Reddit API failed with {resp.status}.")

            data = await resp.json()
            try:
                submission = data[0]["data"]["children"][0]["data"]
            except (KeyError, TypeError, IndexError):
                raise commands.BadArgument("Could not fetch submission.")

            try:
                media = submission["media"]["reddit_video"]
            except (KeyError, TypeError):
                try:
                    # maybe it's a cross post
                    crosspost = submission["crosspost_parent_list"][0]
                    media = crosspost["media"]["reddit_video"]
                except (KeyError, TypeError, IndexError):
                    raise commands.BadArgument("Could not fetch media information.")

            try:
                fallback_url = yarl.URL(media["fallback_url"])
            except KeyError:
                raise commands.BadArgument("Could not fetch fall back URL.")

            return cls(fallback_url)


class DatetimeConverter(commands.Converter[datetime.datetime]):
    @staticmethod
    async def get_timezone(ctx: Context) -> zoneinfo.ZoneInfo | None:
        row: str | None = await ctx.bot.pool.fetchval("SELECT tz FROM tz_store WHERE user_id = $1;", ctx.author.id)
        if row:
            tz = zoneinfo.ZoneInfo(row)
        else:
            tz = zoneinfo.ZoneInfo("UTC")

        return tz

    @classmethod
    async def parse(
        cls,
        argument: str,
        /,
        *,
        ctx: Context,
        timezone: datetime.tzinfo | None = datetime.timezone.utc,
        now: datetime.datetime | None = None,
        duckling_url: yarl.URL,
    ) -> list[tuple[datetime.datetime, int, int]]:
        now = now or datetime.datetime.now(datetime.timezone.utc)

        times: list[tuple[datetime.datetime, int, int]] = []

        async with ctx.bot.session.post(
            duckling_url,
            data={
                "locale": "en_US",
                "text": argument,
                "dims": '["time", "duration"]',
                "tz": str(timezone),
            },
        ) as response:
            data: list[DucklingResponse] = await response.json()

            for time in data:
                if time["dim"] == "time" and "value" in time["value"]:
                    times.append(
                        (
                            datetime.datetime.fromisoformat(time["value"]["value"]),
                            time["start"],
                            time["end"],
                        )
                    )
                elif time["dim"] == "duration":
                    times.append(
                        (
                            datetime.datetime.now(datetime.timezone.utc)
                            + datetime.timedelta(seconds=time["value"]["normalized"]["value"]),
                            time["start"],
                            time["end"],
                        )
                    )

        return times

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> datetime.datetime:
        timezone = await cls.get_timezone(ctx)
        now = ctx.message.created_at.astimezone(tz=timezone)

        duckling_key = ctx.bot.config.get("duckling")
        if not duckling_key:
            raise RuntimeError("No Duckling instance available to perform this action.")

        duckling_url = yarl.URL.build(scheme="http", host=duckling_key["host"], port=duckling_key["port"], path="/parse")

        parsed_times = await cls.parse(argument, ctx=ctx, timezone=timezone, now=now, duckling_url=duckling_url)

        if len(parsed_times) == 0:
            raise commands.BadArgument("Could not parse time.")
        elif len(parsed_times) > 1:
            ...  # TODO: Raise on too many?

        return parsed_times[0][0]


class WhenAndWhatConverter(commands.Converter[tuple[datetime.datetime, str]]):
    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> tuple[datetime.datetime, str]:
        timezone = await DatetimeConverter.get_timezone(ctx)
        now = ctx.message.created_at.astimezone(tz=timezone)

        # Strip some common stuff
        for prefix in ("me to ", "me in ", "me at ", "me that "):
            if argument.startswith(prefix):
                argument = argument[len(prefix) :]
                break

        for suffix in ("from now",):
            if argument.endswith(suffix):
                argument = argument[: -len(suffix)]

        argument = argument.strip()

        duckling_key = ctx.bot.config.get("duckling")
        if not duckling_key:
            raise RuntimeError("No Duckling instance available to perform this action.")

        duckling_url = yarl.URL.build(scheme="http", host=duckling_key["host"], port=duckling_key["port"], path="/parse")

        # Determine the date argument
        parsed_times = await DatetimeConverter.parse(
            argument, ctx=ctx, timezone=timezone, now=now, duckling_url=duckling_url
        )

        if len(parsed_times) == 0:
            raise commands.BadArgument("Could not parse time.")
        elif len(parsed_times) > 1:
            ...  # TODO: Raise on too many?

        when, begin, end = parsed_times[0]

        if begin != 0 and end != len(argument):
            raise commands.BadArgument("Could not distinguish time from argument.")

        if begin == 0:
            what = argument[end + 1 :].lstrip(" ,.!:;")
        else:
            what = argument[:begin].strip()

        for prefix in ("to ",):
            if what.startswith(prefix):
                what = what[len(prefix) :]

        return (when, what or "…")


class BadDatetimeTransform(app_commands.AppCommandError):
    pass


class DatetimeTransformer(app_commands.Transformer):
    @staticmethod
    async def get_timezone(interaction: Interaction) -> zoneinfo.ZoneInfo | None:
        row: str | None = await interaction.client.pool.fetchval(
            "SELECT tz FROM tz_store WHERE user_id = $1;", interaction.user.id
        )
        if row:
            tz = zoneinfo.ZoneInfo(row)
        else:
            tz = zoneinfo.ZoneInfo("UTC")

        return tz

    @classmethod
    async def parse(
        cls,
        argument: str,
        /,
        *,
        interaction: Interaction,
        timezone: datetime.tzinfo | None = datetime.timezone.utc,
        now: datetime.datetime | None = None,
        duckling_url: yarl.URL,
    ) -> list[tuple[datetime.datetime, int, int]]:
        now = now or datetime.datetime.now(datetime.timezone.utc)

        times: list[tuple[datetime.datetime, int, int]] = []

        async with interaction.client.session.post(
            duckling_url,
            data={
                "locale": "en_US",
                "text": argument,
                "dims": '["time", "duration"]',
                "tz": str(timezone),
            },
        ) as response:
            data: list[DucklingResponse] = await response.json()

            for time in data:
                if time["dim"] == "time" and "value" in time["value"]:
                    times.append(
                        (
                            datetime.datetime.fromisoformat(time["value"]["value"]),
                            time["start"],
                            time["end"],
                        )
                    )
                elif time["dim"] == "duration":
                    times.append(
                        (
                            datetime.datetime.now(datetime.timezone.utc)
                            + datetime.timedelta(seconds=time["value"]["normalized"]["value"]),
                            time["start"],
                            time["end"],
                        )
                    )

        return times

    @classmethod
    async def transform(cls, interaction: Interaction, argument: str) -> datetime.datetime:
        timezone = await cls.get_timezone(interaction)
        now = interaction.created_at.astimezone(tz=timezone)

        duckling_key = interaction.client.config.get("duckling")
        if not duckling_key:
            raise RuntimeError("No Duckling instance available to perform this action.")

        duckling_url = yarl.URL.build(scheme="http", host=duckling_key["host"], port=duckling_key["port"], path="/parse")

        parsed_times = await cls.parse(
            argument, interaction=interaction, timezone=timezone, now=now, duckling_url=duckling_url
        )

        if len(parsed_times) == 0:
            raise BadDatetimeTransform("Could not parse time.")
        elif len(parsed_times) > 1:
            ...  # TODO: Raise on too many?

        return parsed_times[0][0]


class WhenAndWhatTransformer(app_commands.Transformer):
    @staticmethod
    async def get_timezone(interaction: Interaction) -> zoneinfo.ZoneInfo | None:
        if interaction.guild is None:
            tz = zoneinfo.ZoneInfo("UTC")
        else:
            row: str | None = await interaction.client.pool.fetchval(
                "SELECT tz FROM tz_store WHERE user_id = $1;",
                interaction.user.id,
            )
            if row:
                tz = zoneinfo.ZoneInfo(row)
            else:
                tz = zoneinfo.ZoneInfo("UTC")

        return tz

    @classmethod
    async def parse(
        cls,
        argument: str,
        /,
        *,
        interaction: Interaction,
        timezone: datetime.tzinfo | None = datetime.timezone.utc,
        now: datetime.datetime | None = None,
        duckling_url: yarl.URL,
    ) -> list[tuple[datetime.datetime, int, int]]:
        now = now or datetime.datetime.now(datetime.timezone.utc)

        times: list[tuple[datetime.datetime, int, int]] = []

        async with interaction.client.session.post(
            duckling_url,
            data={
                "locale": "en_US",
                "text": argument,
                "dims": '["time", "duration"]',
                "tz": str(timezone),
            },
        ) as response:
            data: list[DucklingResponse] = await response.json()

            for time in data:
                if time["dim"] == "time" and "value" in time["value"]:
                    times.append(
                        (
                            datetime.datetime.fromisoformat(time["value"]["value"]),
                            time["start"],
                            time["end"],
                        )
                    )
                elif time["dim"] == "duration":
                    times.append(
                        (
                            datetime.datetime.now(datetime.timezone.utc)
                            + datetime.timedelta(seconds=time["value"]["normalized"]["value"]),
                            time["start"],
                            time["end"],
                        )
                    )

        return times

    @classmethod
    async def transform(cls, interaction: Interaction, value: str) -> datetime.datetime:
        timezone = await cls.get_timezone(interaction)
        now = interaction.created_at.astimezone(tz=timezone)

        # Strip some common stuff
        for prefix in ("me to ", "me in ", "me at ", "me that "):
            if value.startswith(prefix):
                value = value[len(prefix) :]
                break

        for suffix in ("from now",):
            if value.endswith(suffix):
                value = value[: -len(suffix)]

        value = value.strip()

        duckling_key = interaction.client.config.get("duckling")
        if not duckling_key:
            raise RuntimeError("No Duckling instance available to perform this action.")

        duckling_url = yarl.URL.build(scheme="http", host=duckling_key["host"], port=duckling_key["port"], path="/parse")

        parsed_times = await cls.parse(value, interaction=interaction, timezone=timezone, now=now, duckling_url=duckling_url)

        if len(parsed_times) == 0:
            raise BadDatetimeTransform("Could not parse time.")
        elif len(parsed_times) > 1:
            ...  # TODO: Raise on too many?

        when, begin, end = parsed_times[0]

        if begin != 0 and end != len(value):
            raise BadDatetimeTransform("Could not distinguish time from argument.")

        if begin == 0:
            what = value[end + 1 :].lstrip(" ,.!:;")
        else:
            what = value[:begin].strip()

        for prefix in ("to ",):
            if what.startswith(prefix):
                what = what[len(prefix) :]

        return when


# This is because Discord is stupid with Slash Commands and doesn't actually have integer types.
# So to accept snowflake inputs you need a string and then convert it into an integer.
class Snowflake:
    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> int:
        try:
            return int(argument)
        except ValueError:
            param = ctx.current_parameter
            if param:
                raise commands.BadArgument(f"{param.name} argument expected a Discord ID not {argument!r}")
            raise commands.BadArgument(f"expected a Discord ID not {argument!r}")


class MystbinPasteConverter(commands.Converter[str]):
    async def convert(self, ctx: GuildContext, argument: str) -> str:
        matches = MYSTBIN_REGEX.search(argument)
        if not matches:
            raise commands.ConversionError(self, ValueError("No Mystbin IDs found in this text."))

        return matches["id"]
