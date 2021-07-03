import json
import random
import os
from base64 import b64decode
from functools import wraps
import boto3 as boto3
import feedparser
import logging
import time
from botocore.exceptions import ClientError
from telegram import Bot, ParseMode, constants, error
from bs4 import BeautifulSoup

logger = logging.getLogger()
logger.setLevel(logging.INFO)
verbose = False

MILLIS_IN_HOUR = 3.6e+6
channel_id = os.environ['channelId']
sm_client = boto3.client('secretsmanager')
tbot = None
ddb_client = boto3.resource("dynamodb", region_name="eu-south-1").Table(os.environ["tableName"])


def retry(ExceptionToCheck, tries=8, delay=2, backoff=2, first_delay=0, logger=None):
    """Retry calling the decorated function using an exponential backoff.

    :param ExceptionToCheck: the exception to check. may be a tuple of
        exceptions to check
    :type ExceptionToCheck: Exception or tuple
    :param tries: number of times to try (not retry) before giving up
    :type tries: int
    :param delay: initial delay between retries in seconds
    :type delay: int
    :param backoff: backoff multiplier e.g. value of 2 will double the delay
        each retry
    :type backoff: int
    :param first_delay: delay of the first try
    :type first_delay: int
    """

    def deco_retry(f):

        @wraps(f)
        def f_retry(*args, **kwargs):
            mtries, mdelay = tries, delay
            while mtries > 1:
                # print 'mtries: {}'.format(mtries)
                try:
                    # launch the first time using little random delay
                    if first_delay and mtries == tries:
                        time.sleep(random.randint(0, first_delay))
                    return f(*args, **kwargs)
                except ExceptionToCheck as e:

                    if e.response['Error']['Code'] == 'RequestLimitExceeded':
                        msg = "%s, Retrying in %d seconds..." % (str(e), mdelay)
                        logger.info(msg)

                        # print 'mdelay: {}'.format(mdelay)
                        rand_delay = random.randint(0, mdelay)
                        # print 'random delay: {}'.format(rand_delay)
                        time.sleep(rand_delay)
                        mtries -= 1
                        mdelay *= backoff

                    else:
                        logger.info('Error occured during backoff: %s' % (str(e)))
                        raise e
            return f(*args, **kwargs)

        return f_retry  # true decorator

    return deco_retry


@retry((ClientError), logger=logger)
def _get_item(ddb_client, *args, **kwargs):
    return ddb_client.get_item(*args, **kwargs)


@retry((ClientError), logger=logger)
def _put_item(ddb_client, *args, **kwargs):
    return ddb_client.put_item(*args, **kwargs)


@retry((ClientError), first_delay=0, logger=logger)
def sm_get_secret_value(sm_client, **kwargs):
    return sm_client.get_secret_value(**kwargs)


def decode_secret_from_secretsmanager(response):
    if 'SecretString' in response:
        if verbose:
            logger.info("Secret is of type string")
        secret = response['SecretString']
    else:
        if verbose:
            logger.info("Secret is binary encoded")
        secret = b64decode(response['SecretBinary'])
    return secret


def send_message(chat_id, msg):
    sent = False
    while not sent:
        try:
            tbot.send_message(chat_id=chat_id, parse_mode=ParseMode.HTML, text=msg)
            sent = True
        except error.RetryAfter as e:
            logger.info(str(e))
            time.sleep(e.retry_after)


def published(ddb_client, entry_id):
    item = _get_item(ddb_client, Key={"id": entry_id})
    if verbose:
        logger.info(json.dumps(item))
    return "Item" in item


def build_message(feed):
    logging.info("Building message.")

    title = '<b><i>' + feed.title + '</i></b>'
    summary = BeautifulSoup(feed.summary, features='html.parser').get_text()
    link = feed.link

    msg = title + '.\n\n' + summary + '\n\n' + link
    if len(msg) > constants.MAX_MESSAGE_LENGTH:
        summary = summary[
                  :constants.MAX_MESSAGE_LENGTH - len(link) - 8] + '...'  # 8 = len(.\n\n) + len(\n\n) + len(...)

    msg = title + '.\n\n' + summary + '\n\n' + link

    logging.info(f"Message built: {msg}")

    return msg


def handler(event, context):
    global tbot

    tbot = Bot(
        token=decode_secret_from_secretsmanager(
            sm_get_secret_value(sm_client, SecretId=os.environ['TELEGRAM_API_KEY_SECRET'])
        )
    )

    feed_url = event['feedUrl']

    logging.info(f'Received event: {event}')

    feeds = feedparser.parse(feed_url)

    for feed in feeds.entries:
        if not published(ddb_client, feed.id):
            msg = build_message(feed)
            try:
                logging.info("Sending message to channel...")
                send_message(chat_id=channel_id, msg=msg)
                one_month_from_now = time.time() + (60 * 60 * 24 * 30 * 1000)
                _put_item(ddb_client,
                          Item={"id": feed.id, "title": feed.title, "link": feed.link, "ttl": int(one_month_from_now)})
            except error.RetryAfter as e:
                logger.info(str(e))
                time.sleep(e.retry_after)

    logging.info("Finished sending messages. Exiting...")
