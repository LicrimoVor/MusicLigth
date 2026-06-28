def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def color_to_hex(color):
    return "#{:02x}{:02x}{:02x}".format(*[int(clamp(channel, 0, 255)) for channel in color])
