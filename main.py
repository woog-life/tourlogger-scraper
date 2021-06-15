import inspect
import logging
import os
import socket
import sys
from datetime import datetime
from typing import Tuple, Optional, Callable, Union, NewType, List

import pytz
import requests
import urllib3
from bs4 import BeautifulSoup, Tag
from telegram import Bot

TOWN_NAME = os.getenv("TOWN_NAME") or os.getenv("CUXHAVEN_NAME") or "cuxhaven"
WOOG_TEMPERATURE_URL = f"https://www.tourlogger.de/wassertemperatur/{TOWN_NAME}/"
# noinspection HttpUrlsUsage
# cluster internal communication
BACKEND_URL = os.getenv("BACKEND_URL") or "http://api:80"
BACKEND_PATH = os.getenv("BACKEND_PATH") or "lake/{}/temperature"
UUID = os.getenv("TOWN_UUID") or os.getenv("CUXHAVEN_UUID")
API_KEY = os.getenv("API_KEY")

WATER_INFORMATION = NewType("WaterInformation", Tuple[str, float])


def create_logger(name: str, level: int = logging.DEBUG) -> logging.Logger:
    logger = logging.Logger(name)
    ch = logging.StreamHandler(sys.stdout)

    formatting = "[{}] %(asctime)s\t%(levelname)s\t%(module)s.%(funcName)s#%(lineno)d | %(message)s".format(name)
    formatter = logging.Formatter(formatting)
    ch.setFormatter(formatter)

    logger.addHandler(ch)
    logger.setLevel(level)

    return logger


def send_telegram_alert(message: str, token: str, chatlist: List[str]):
    logger = create_logger(inspect.currentframe().f_code.co_name)
    if not token:
        logger.error("TOKEN not defined in environment, skip sending telegram message")
        return

    if not chatlist:
        logger.error("chatlist is empty (env var: TELEGRAM_CHATLIST)")

    for user in chatlist:
        Bot(token=token).send_message(chat_id=user, text=f"Error while executing: {message}")


def get_website() -> Tuple[str, bool]:
    logger = create_logger(inspect.currentframe().f_code.co_name)
    url = WOOG_TEMPERATURE_URL

    logger.debug(f"Requesting {url}")
    response = requests.get(url)

    content = response.content.decode("utf-8")
    logger.debug(content)

    return content, True


def parse_website_xml(xml: str) -> BeautifulSoup:
    return BeautifulSoup(xml, "html.parser")


def get_temperature(html: BeautifulSoup):
    logger = create_logger(inspect.currentframe().f_code.co_name)

    table = html.find("dl", {"class": "tourlogger-description-list"})
    if not table:
        logger.error(f"table not found in html {html}")
        return None

    header_elements = table.find_all("dt")
    if not header_elements or len(header_elements) < 2:
        logger.error(f"tr not found or len(dt) < 2 in {table}")
        return None
    try:
        today_index: int = [idx for (idx, row) in enumerate(header_elements) if "Heute" in row.text][0]
    except IndexError:
        logger.error("no dt element with `Heute` text has been found")
        return None

    value_elements = table.find_all("dd")
    if not value_elements or len(value_elements) < (today_index + 1):
        logger.error(f"dd not found or len(dd) < 2 in {table}")
        return None
    try:
        temperature = float(value_elements[today_index].text.split(" ")[0])
        return temperature
    except (IndexError, ValueError) as e:
        logger.error(f"index or value error while accessing temperature: {e}")
        return None


def send_data_to_backend(water_information: WATER_INFORMATION) -> Tuple[
    Optional[requests.Response], str]:
    logger = create_logger(inspect.currentframe().f_code.co_name)
    path = BACKEND_PATH.format(UUID)
    url = "/".join([BACKEND_URL, path])

    water_timestamp, water_temperature = water_information
    if water_temperature <= 0:
        return None, "water_temperature is <= 0, please approve this manually."

    headers = {"Authorization": f"Bearer {API_KEY}"}
    data = {"temperature": water_temperature, "time": water_timestamp}
    logger.debug(f"Send {data} to {url}")

    try:
        response = requests.put(url, json=data, headers=headers)
        logger.debug(f"success: {response.ok} | content: {response.content}")
    except (requests.exceptions.ConnectionError, socket.gaierror, urllib3.exceptions.MaxRetryError):
        logger.exception(f"Error while connecting to backend ({url})", exc_info=True)
        return None, url

    return response, url


def main() -> Tuple[bool, str]:
    logger = create_logger(inspect.currentframe().f_code.co_name)
    content, success = get_website()
    if not success:
        message = f"Couldn't retrieve website: {content}"
        logger.error(message)
        return False, message

    soup = parse_website_xml(content)
    temperature = get_temperature(soup)
    if not temperature:
        message = "Couldn't retrieve temperature"
        logger.error(message)
        return False, message

    time = datetime.now().replace(hour=0, minute=0, second=0)
    local = pytz.timezone("Europe/Berlin")
    time = local.localize(time)
    iso_time: str = time.astimezone(pytz.utc).isoformat()
    water_information = (iso_time, temperature)

    response, generated_backend_url = send_data_to_backend(water_information)

    if not response or not response.ok:
        message = f"Failed to put data ({water_information}) to backend: {generated_backend_url}\n{response.content}"
        logger.error(message)
        return False, message

    return True, ""


root_logger = create_logger("__main__")

if not UUID:
    root_logger.error("TOWN_UUID not defined in environment")
elif not API_KEY:
    root_logger.error("API_KEY not defined in environment")
else:
    success, message = main()
    if not success:
        root_logger.error(f"Something went wrong ({message})")
        token = os.getenv("TOKEN")
        chatlist = os.getenv("TELEGRAM_CHATLIST") or "139656428"
        send_telegram_alert(message, token=token, chatlist=chatlist.split(","))
        sys.exit(1)
