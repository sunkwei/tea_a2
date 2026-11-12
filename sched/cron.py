#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cron expressions: when something happens, down to the minute.

The queue's scheduler asks this module two questions:

  * is this minute one of the minutes the expression names? (fire)
  * when is the next such minute? (what the page shows)

The expression is the classic five fields:

    minute  hour  day-of-month  month  day-of-week

    */5 * * * *      every five minutes
    30 9 * * 1-5    every working day at 09:30
    0 0 1 * *       the first of every month at midnight
    15,45 * * * *   twice an hour, at :15 and :45

The finest grain is **one minute**: the minute field is the smallest one, so
"every minute" is the fastest thing a schedule can do. There is no second
field - a thing that wants seconds does not want a cron.

The times are **local times** (the machine's own clock), like the crontab of
a person - not the UTC stamps the queue writes into its records.

Standard library only: the queue runs on devices where a pip install is not
an option.
"""

import datetime
import time

# The five fields of an expression and the values they may take. The day of
# the week is 0 (Sunday) to 6, like cron; 7 is Sunday as well and is folded
# into 0 while parsing.
FIELD_NAMES = ("minute", "hour", "day of month", "month", "day of week")
RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

MONTH_NAMES = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5,
             "sat": 6}

# The macros a crontab knows.
MACROS = {
    "yearly": "0 0 1 1 *",
    "annually": "0 0 1 1 *",
    "monthly": "0 0 1 * *",
    "weekly": "0 0 * * 0",
    "daily": "0 0 * * *",
    "midnight": "0 0 * * *",
    "hourly": "0 * * * *",
}

# How far the search for the next (or the previous) match may go: five
# years. A schedule that matches once every five years is still a schedule.
HORIZON_DAYS = 366 * 5


class CronError(Exception):
    """The expression cannot be read: the message says what is wrong."""


def _value(text, low, high, names=None):
    """One number of a field: a number or a name ("jan", "mon")."""
    text = str(text or "").strip().lower()
    if not text:
        raise CronError("少了一个值")
    if names and text in names:
        number = names[text]
    else:
        try:
            number = int(text)
        except ValueError:
            raise CronError("看不懂的值：%r" % text)
    if number < low or number > high:
        raise CronError("%d 不在 %d-%d 之间" % (number, low, high))
    return number


def _field(text, index):
    """One field of the expression -> the set of the values it means.

    What a field may say: "*" (all), "5" (one), "1-5" (a range), "*/10"
    (every tenth), "1-30/5" (every fifth of the range), "1,2,3" (a list) -
    and the names of the months and of the weekdays.
    """
    low, high = RANGES[index]
    names = None
    if index == 3:
        names = MONTH_NAMES
    elif index == 4:
        names = DOW_NAMES
    values = set()
    for part in str(text or "").split(","):
        part = part.strip()
        if not part:
            raise CronError("空的字段")
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            part = part.strip()
            try:
                step = int(step_text.strip())
            except ValueError:
                raise CronError("步长不是数字：%r" % step_text)
            if step <= 0:
                raise CronError("步长要大于 0：%r" % step_text)
        if part in ("*", "?"):
            start, end = low, high
        elif "-" in part:
            head, _, tail = part.partition("-")
            start = _value(head, low, high, names)
            end = _value(tail, low, high, names)
            if start > end:
                raise CronError("区间的头比尾大：%r" % part)
        else:
            start = _value(part, low, high, names)
            # "5/10" means 5, 15, 25, ... - up to the end of the range.
            end = start if step == 1 else high
        for number in range(start, end + 1, step):
            values.add(number)
    if index == 4 and 7 in values:
        # 7 is Sunday as well (some crontabs write it that way).
        values.discard(7)
        values.add(0)
    return values


class Cron(object):
    """One parsed expression."""

    def __init__(self, expression, minutes, hours, doms, months, dows,
                 dom_star, dow_star):
        self.expression = expression
        self.minutes = minutes
        self.hours = hours
        self.doms = doms
        self.months = months
        self.dows = dows
        # A field that says "*" is not the same as one that lists every
        # value: the day-of-month and the day-of-week are OR-ed when both
        # of them are restricted (that is what cron does).
        self.dom_star = dom_star
        self.dow_star = dow_star
        self._minute_list = sorted(minutes)
        self._hour_list = sorted(hours)
        # The answers of the last minute are kept: the queue asks the same
        # question every round, and the answer only changes when the minute
        # changes.
        self._prev_cache = {}
        self._next_cache = {}

    def __repr__(self):
        return "Cron(%r)" % (self.expression,)

    def __str__(self):
        return self.expression

    # ---- the questions ---------------------------------------------------
    def matches(self, tm):
        """Does this local time (a time.struct_time) match?"""
        if tm.tm_mon not in self.months:
            return False
        if self.dom_star and self.dow_star:
            return True
        dom_ok = tm.tm_mday in self.doms
        # tm_wday counts from Monday; cron counts from Sunday.
        dow_ok = ((tm.tm_wday + 1) % 7) in self.dows
        if self.dom_star:
            return dow_ok
        if self.dow_star:
            return dom_ok
        return dom_ok or dow_ok

    def matches_epoch(self, epoch):
        """Does this moment (epoch seconds) match?"""
        return self.matches(time.localtime(int(epoch)))

    def next_after(self, epoch):
        """The epoch of the first matching minute after `epoch`.

        None when the expression does not match anything within the horizon
        (five years) - which cannot happen for a well-formed expression,
        but a schedule that says "0 0 30 2 *" (the 30th of February) would.
        """
        key = int(epoch) // 60
        if key in self._next_cache:
            return self._next_cache[key]
        result = self._next_after(int(epoch))
        self._next_cache = {key: result}
        return result

    def _next_after(self, epoch):
        start = (int(epoch) // 60 + 1) * 60
        moment = start
        for _ in range(HORIZON_DAYS):
            tm = time.localtime(moment)
            if tm.tm_mon in self.months and self._day_ok(tm):
                for cand in self._times_of_day(tm.tm_year, tm.tm_mon, tm.tm_mday,
                                               min_hour=tm.tm_hour):
                    if cand >= start:
                        return cand
            moment = _next_midnight(moment)
        return None

    def prev_at_or_before(self, epoch):
        """The epoch of the last matching minute at or before `epoch`.

        None when there is no such minute within the horizon.
        """
        key = int(epoch) // 60
        if key in self._prev_cache:
            return self._prev_cache[key]
        result = self._prev_at_or_before(int(epoch))
        self._prev_cache = {key: result}
        return result

    def _prev_at_or_before(self, epoch):
        moment = (int(epoch) // 60) * 60
        for _ in range(HORIZON_DAYS):
            tm = time.localtime(moment)
            if tm.tm_mon in self.months and self._day_ok(tm):
                # The last one of the day that is not in the future yet.
                for cand in reversed(self._times_of_day(tm.tm_year, tm.tm_mon,
                                                        tm.tm_mday,
                                                        max_hour=tm.tm_hour)):
                    if cand <= moment:
                        return cand
            moment = _prev_end_of_day(moment)
        return None

    # ---- the helpers -----------------------------------------------------
    def _day_ok(self, tm):
        return self.matches(tm)

    def _times_of_day(self, year, month, day, min_hour=None, max_hour=None):
        """The matching times of that local day, as epochs, sorted.

        Only the hours that can hold the answer are looked at: the search
        for the next minute does not need the hours that are already past.
        """
        out = []
        for hour in self._hour_list:
            if min_hour is not None and hour < min_hour:
                continue
            if max_hour is not None and hour > max_hour:
                continue
            for minute in self._minute_list:
                out.append(int(time.mktime((year, month, day, hour, minute,
                                             0, 0, 0, -1))))
        out.sort()
        return out


def _next_midnight(moment):
    """The epoch of the next local midnight."""
    tm = time.localtime(moment)
    day = datetime.date(tm.tm_year, tm.tm_mon, tm.tm_mday) + datetime.timedelta(days=1)
    return int(time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1)))


def _prev_end_of_day(moment):
    """The epoch of the last minute of the previous local day."""
    tm = time.localtime(moment)
    day = datetime.date(tm.tm_year, tm.tm_mon, tm.tm_mday) - datetime.timedelta(days=1)
    return int(time.mktime((day.year, day.month, day.day, 23, 59, 0, 0, 0, -1)))


def parse(expression):
    """The expression -> a Cron. Raises CronError when it cannot be read."""
    text = str(expression or "").strip()
    if not text:
        raise CronError("空的表达式")
    lowered = text.lower()
    if lowered.startswith("@"):
        macro = MACROS.get(lowered[1:])
        if not macro:
            raise CronError("不认识的写法：%r（认识的：%s）"
                            % (text, "、".join("@" + name for name in sorted(MACROS))))
        text = macro
    parts = text.split()
    if len(parts) != 5:
        raise CronError("要 5 段（分 时 日 月 周），给了 %d 段：%r"
                        % (len(parts), expression))
    return Cron(
        " ".join(parts),
        _field(parts[0], 0),
        _field(parts[1], 1),
        _field(parts[2], 2),
        _field(parts[3], 3),
        _field(parts[4], 4),
        parts[2] in ("*", "?"),
        parts[4] in ("*", "?"),
    )


def parse_safe(expression):
    """The expression -> a Cron, or None when it cannot be read."""
    try:
        return parse(expression)
    except CronError:
        return None
