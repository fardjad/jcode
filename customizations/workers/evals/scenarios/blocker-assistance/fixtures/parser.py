def parse(line):
    name, value = line.split(":")
    return name, int(value)
