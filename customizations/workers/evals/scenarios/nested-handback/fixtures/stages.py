def split_record(line):
    return line.split(":")


def clean_record(parts):
    name, quantity = parts
    return name, quantity
