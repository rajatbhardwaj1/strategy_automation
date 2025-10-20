

def send_telegram_message(message):
    print(f"Sending telegram msg {message}")


import yaml

def load_config(path="src/config.yaml"):
    with open(path, "r") as f:
        config = yaml.safe_load(f)
    return config["trading"]