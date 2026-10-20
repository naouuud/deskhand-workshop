import glob


def write_policies_to_txt():
    """
    Consolidate policy files into single txt file for token count.
    """
    text = ""
    for path in glob.glob("../shared/policies/*.md"):
        text += open(path, encoding="utf-8").read() + "\n\n"

    with open("policies.txt", "w", encoding="utf-8") as file:
        file.write(text)


write_policies_to_txt()
