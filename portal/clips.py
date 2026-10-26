"""Shared clip and fade validation. Fade times are relative to the selected clip."""
import math


def default_clip(total):
    return {"start": 0, "duration": min(12, total), "fade_in": 0, "fade_out": 0}


def clip_values(data, total, existing=None):
    previous = default_clip(total) | (existing or {})
    values = {}
    for key in ("start", "duration", "fade_in", "fade_out"):
        try:
            value = data.get(key, previous[key])
            if isinstance(value, bool):
                raise ValueError
            value = float(value)
            if not math.isfinite(value):
                raise ValueError
            values[key] = round(value, 3)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("Clip times and fades must be valid numbers.") from None
    start, duration = values["start"], values["duration"]
    if start < 0 or start >= total or duration <= 0 or start + duration > total + .025:
        raise ValueError("Keep the selected clip within the audio file, with a duration greater than zero.")
    duration = round(min(duration, total - start), 3)
    if duration <= 0:
        raise ValueError("The selected clip must be at least 0.001 seconds long.")
    values["duration"] = duration
    if values["fade_in"] < 0 or values["fade_out"] < 0:
        raise ValueError("Fade times cannot be negative. Use 0 to turn a fade off.")
    fade_total = values["fade_in"] + values["fade_out"]
    if fade_total > duration + .000001:
        if "fade_in" not in data and "fade_out" not in data:
            # Older clients and a shorter trim retain both fades proportionally.
            values["fade_in"] = round(values["fade_in"] * duration / fade_total, 3)
            values["fade_out"] = round(duration - values["fade_in"], 3)
        else:
            raise ValueError("Fade-in and fade-out must fit inside the selected clip.")
    return values


def fade_filter(duration, fade_in=0, fade_out=0):
    """FFmpeg/ffplay audio filter, with seek-independent timestamps and linear fades."""
    values = (float(duration), float(fade_in), float(fade_out))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Fade times must be finite numbers.")
    duration, fade_in, fade_out = values
    if duration <= 0 or fade_in < 0 or fade_out < 0 or fade_in + fade_out > duration + .000001:
        raise ValueError("Fade times must fit inside the selected clip.")
    if not fade_in and not fade_out:
        return ""
    number = lambda value: f"{value:.6f}".rstrip("0").rstrip(".") or "0"
    filters = ["asetpts=PTS-STARTPTS"]
    if fade_in:
        filters.append(f"afade=t=in:st=0:d={number(fade_in)}:curve=tri")
    if fade_out:
        filters.append(f"afade=t=out:st={number(duration - fade_out)}:d={number(fade_out)}:curve=tri")
    return ",".join(filters)
