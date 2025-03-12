from django.core.management.base import BaseCommand, CommandError
from FeedManager.models import ProcessedFeed, OriginalFeed, Article
import feedparser
from datetime import datetime
import pytz
import re
import os
from django.conf import settings
from django.utils import timezone
from FeedManager.utils import passes_filters, match_content, generate_untitled, clean_url, generate_summary
import logging
from django.db import transaction
import requests
from fake_useragent import UserAgent
import httpx
import time
import json
from datetime import timedelta

logger = logging.getLogger('feed_logger')


def fetch_feed(url: str, last_modified: datetime):
    '拉取单个 original feed 的更新'
    logger.debug(f'                [*] fetch feed for {url}')
    headers = {}
    ua = UserAgent()
    # Try comment out the following line to see if it works
    if last_modified:
        headers['If-Modified-Since'] = last_modified.strftime('%a, %d %b %Y %H:%M:%S GMT')
    headers['User-Agent'] = ua.random.strip()
    try:
#        print(time.time())
        response = requests.get(url, headers=headers, timeout=30)
#        print(time.time())
        if response.status_code == 200:
            feed = feedparser.parse(response.text)
            logger.debug(f"                [*] Response status: {response.status_code}, Headers: {response.headers}")
            # todo 需要丰富判断逻辑，安全客没有设置Last-Modified，每次都是200导致反复读取数据库
            
            # 处理源未正常返回304的情况，如果和上次更新时间一样就返回not modified
            last_modified_key = 'Last-Modified' if ('api.anquanke.com' not in url and 'therecord.media' not in url) else 'Date' # 安全客的key不标准
            if last_modified and response.headers.get(last_modified_key):
                last_modified_response = datetime.strptime(response.headers.get(last_modified_key), '%a, %d %b %Y %H:%M:%S GMT').replace(tzinfo=pytz.UTC)
                logger.debug(f"                [*] 源的更新时间: {last_modified_response}")
                logger.debug(f"                [*] feed的上次更新时间: {last_modified}")
                logger.debug(f"                [*] 分钟级对比 上次: {last_modified.strftime('%Y-%m-%d %H:%M')}   最新: {last_modified_response.strftime('%Y-%m-%d %H:%M')}")
                if last_modified.strftime('%Y-%m-%d %H:%M') == last_modified_response.strftime('%Y-%m-%d %H:%M'): # 对比分钟级时间是否相同，因为安全客每时每刻都在刷新Last-Modified字段
                    logger.debug(f"                [*] 内容没有更新")
                    return {'feed': None, 'status': 'not_modified', 'last_modified': response.headers.get(last_modified_key)}

            # # 处理源未正常返回304的情况，根据processed feed的上次更新时间和当前时间判断，这样也有问题，processed feed是所有feed的最早更新时间，和当前的时间当然差大于1分钟。
            # if last_modified :
            #     logger.debug(f"                [*] 现在的时间: {timezone.now()}")
            #     logger.debug(f"                [*] processed feed的上次更新时间: {last_modified}")
            #     logger.debug(f"                [*] 时间差: {timezone.now() - last_modified}")
            #     logger.debug(f"                [*] 距离上次更新不到1分钟 暂不更新")
            #     if (timezone.now() - last_modified < timedelta(minutes=1)):
            #         return {'feed': None, 'status': 'not_modified', 'last_modified': response.headers.get('Last-Modified')}

            return {'feed': feed, 'status': 'updated', 'last_modified': response.headers.get(last_modified_key)}
        elif response.status_code == 304:
            # ! Why is it taking so long to show not_modified? 8 seconds
            # Maybe it's because of the User-Agent or the If-Modified-Since header?
            #print(time.time())
            return {'feed': None, 'status': 'not_modified', 'last_modified': response.headers.get('Last-Modified')}
        else:
            logger.error(f'                [*] Failed to fetch feed {url}: {response.status_code}')
            return {'feed': None, 'status': 'failed'}

    except Exception as e:
        logger.error(f'                [*] Failed to fetch feed {url}: {str(e)}')
        return {'feed': None, 'status': 'failed'}

class Command(BaseCommand):
    help = 'Updates and processes RSS feeds based on defined schedules and filters.'

    def add_arguments(self, parser):
        parser.add_argument('-n', '--name', type=str, help='Name of the ProcessedFeed to update')

    def handle(self, *args, **options):
        feed_name = options.get('name')
        if feed_name:
            try:
                feed = ProcessedFeed.objects.get(name=feed_name)
                logger.info(f'[start] Processing single feed: {feed.name} at {timezone.now()}')
                self.update_feed(feed)
            except ProcessedFeed.DoesNotExist:
                raise CommandError('[ end ] ProcessedFeed "%s" does not exist' % feed_name)
            except Exception as e:
                logger.error(f'[ end ] Error processing feed {feed_name}: {str(e)}')
        else:
            processed_feeds = ProcessedFeed.objects.all()
            for feed in processed_feeds:
                try:
                    logger.info(f'[start] Processing feed: {feed.name} at {timezone.now()}')
                    self.update_feed(feed)
                    logger.info(f'[ end ] Processing feed: {feed.name} at {timezone.now()}')
                except Exception as e:
                    logger.error(f'[ end ]Error processing feed {feed.name}: {str(e)}')
                    continue  # make sure to continue to the next feed

    def update_feed(self, feed):
        'feed : 处理的是processed_feed '
        self.current_n_processed = 0
        entries = []
        current_modified = feed.last_modified
        min_new_modified = None
        logger.debug(f'            [*] update_feed start   Current last modified: {current_modified} for feed {feed.name}')
        for original_feed in feed.feeds.all():
            feed_data = fetch_feed(original_feed.url, original_feed.last_modified)

            # update feed.last_modified based on earliest last_modified of all original_feeds
            if feed_data['status'] == 'updated':
                original_feed.valid = True
                original_feed.save()
                logger.debug(f'                [*] Feed {original_feed.url} updated, the new modified time is {feed_data["last_modified"]}')
                # 更新 original_feed 的 last_modified   
                
                original_feed.last_modified = datetime.strptime(feed_data['last_modified'], '%a, %d %b %Y %H:%M:%S GMT').replace(tzinfo=pytz.UTC) if feed_data['last_modified'] else None
                original_feed.save()
                
                new_modified = datetime.strptime(feed_data['last_modified'], '%a, %d %b %Y %H:%M:%S GMT').replace(tzinfo=pytz.UTC) if feed_data['last_modified'] else None
                if new_modified and (not min_new_modified or new_modified < min_new_modified):
                    # 使用多个源中最早的时间做为last_modified，为了确保不会遗漏任何更新。
                    min_new_modified = new_modified

                parsed_feed = feed_data['feed']
                # first sort by published date, then only process the most recent max_articles_to_keep articles
                if parsed_feed.entries: 
                    parsed_feed.entries.sort(key=lambda x: x.get('published_parsed', []), reverse=True)
                    # self.stdout.write(f'  Found {len(parsed_feed.entries)} entries in feed {original_feed.url}')
                    entries.extend((entry, original_feed) for entry in parsed_feed.entries[:original_feed.max_articles_to_keep])
            elif feed_data['status'] == 'not_modified':
                original_feed.valid = True
                original_feed.save()
                logger.debug(f'                [-] Feed {original_feed.url} not modified')
                logger.debug(f'                [-] Feed {original_feed.url} modified time is {feed_data["last_modified"]} and the current feed modified time is {current_modified}')
                continue
            elif feed_data['status'] == 'failed':
                logger.error(f'                [-] Failed to fetch feed {original_feed.url}')
                original_feed.valid = False
                original_feed.save()
                continue

        if min_new_modified:
            feed.last_modified = min_new_modified
            logger.debug(f'            [*] 调用了save函数 for feed {min_new_modified}')
            feed.save() # 会出现update_feeds任务的重新调用，注意不要出现死循环。

        entries.sort(key=lambda x: x[0].get('published_parsed', timezone.now().timetuple()), reverse=True)
        for entry, original_feed in entries: # todo
            try:
                self.process_entry(entry, feed, original_feed)
            except Exception as e:
                # logger.error(f'Failed to process entry: {str(e)}')
                # 把报错的trackback.print_exc()打印出来
                 logger.error(f'                  [-] Failed to process entry: {str(e)}', exc_info=True)
                #  logger.error(f'------Failed to process entry: {e.__traceback__}') 
                #  import sys
                #  sys.exit(1)
        logger.debug(f'            [*] update_feed end   {feed.name}')

    def process_entry(self, entry, feed, original_feed):
        '对于 '
        # 先检查 filter 再检查数据库
        if passes_filters(entry, feed, 'feed_filter'):
            existing_article = Article.objects.filter(link=clean_url(entry.link), original_feed=original_feed).first()
            logger.debug(f'                  [-] Already in db: {entry.title}' if existing_article else f'                  [-] Processing new article: {entry.title}')
            if not existing_article:
                # 第一步：补全原文并存储
                ## 如果原文没爬下来，访问原文补全原文
                if '安全客' in original_feed.title:
                    content = (entry.content if 'content' in entry else (entry.description if 'description' in entry else ''))
                else:
                    content = (entry.content[0].value if 'content' in entry else (entry.description if 'description' in entry else ''))
                # todo 定制化
                if (content == '' or len(content) < 500) and 'TheHackersNews' not in original_feed.url:
                    ua = UserAgent()
                    headers = {}
                    headers['User-Agent'] = ua.random.strip()
                    logger.debug(f'                    [-] fetch full content for : {entry.link}')
                    content = requests.get(entry.link, headers=headers, timeout=30).text

                # 清理内容，处理制表符和其他特殊字符
                content = content.replace('\t', '    ')  # 将制表符替换为4个空格
                content = ' '.join(content.split())  # 规范化空白字符
                
                # 处理自动补全标记
                content = re.sub(r'\u001b\[[0-9;]*[a-zA-Z]', '', content)  # 移除 ANSI 转义序列
                content = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', content)     # 移除控制字符

                ## 如果文章不存在，则创建新文章
                article = Article(
                    original_feed=original_feed,
                    title=generate_untitled(entry),
                    link=clean_url(entry.link),
                    published_date=datetime(*entry.published_parsed[:6], tzinfo=pytz.UTC) if 'published_parsed' in entry else timezone.now(),
                    content=content
                )
                article.save()
                # 注意这里的缩进，如果已经存在 Database 中的文章（非新文章），那么就不需要浪费 token 总结了
#            else:
#                article = existing_article
                 # 第二步：过滤文章
                if self.current_n_processed < feed.articles_to_summarize_per_interval and passes_filters(entry, feed, 'summary_filter'): # and not article.summarized:
                    
                    # 第三步：为每篇文章生成summary AI 
                    logger.info(f'                    [-] 生成 summary for : {article.title}')
                    # prompt = f"Please summarize this article, and output the result only in JSON format. First item of the json is a one-line summary in 15 words named as 'summary_one_line', second item is the 150-word summary named as 'summary_long', third item is the translated article title as 'title'. Output result in {feed.summary_language} language."
                    prompt = f"请总结这篇文章，并仅以 JSON 格式输出结果。JSON 的第一项是名为 “summary_one_line” 的 15 字单行总结，第二项是名为 “summary_long” 的 200字以内的总结（也就是summary），第三项是翻译后的文章标题，名为 “title”，第三项是文章标签，名为 “tag”，以中文语言输出结果"
                    output_mode = 'json'
                    if feed.additional_prompt:
                        prompt = f"{ prompt + feed.additional_prompt}"
                        # output_mode = 'json'
                    summary_results = generate_summary(article, feed.model, output_mode, prompt, feed.other_model)
                    # TODO the JSON mode parse is hard-coded as is the default prompt, maybe support automatic json parsing in the future
                    try:
                        # article.summary = summary_results # 无论咋样都村summary里
                        json_result = json.loads(summary_results)
                        article.summary = json_result['summary_long']
                        article.summary_one_line = json_result['summary_one_line']
                        # if feed.translate_title:
                        article.title = json_result['title']
                        article.tag = json_result['tag']
                        article.summarized = True
                        article.custom_prompt = False
                        logger.info(f'                    [-] Summary generated for article: {article.title}')
                        article.save()
                    except:
                        article.summary = summary_results
                        article.summarized = True
                        article.custom_prompt = True
                        logger.info(f'                    [-] Summary generated for article: {article.title}')
                        article.save()
                    self.current_n_processed += 1
            # else:
            #     logger.debug(f'  Already in db: {entry.title}' if existing_article else f'  Processing new article: {entry.title}')
            #     # 如果已经存在，则更新文章, 存储访问原文链接获取的内容
            #     if entry.content:
            #         # logger.info(f' -------- 存储: {entry.link} 的内容')
            #         existing_article.content = entry.content
            #         existing_article.save()

    def fetch_feed(self, url):
        # 对于 feedburner 源特殊处理，hackernews的域名
        if 'feedburner.com' in url:
            # 使用内容比较而不是 HTTP 状态码
            response = requests.get(url)
            content_hash = hashlib.md5(response.content).hexdigest()
            
            # 如果内容没变，模拟 304 响应
            if content_hash == self.last_content_hash.get(url):
                return {'status': 'not_modified'}
            
            self.last_content_hash[url] = content_hash
            return {
                'status': 'updated',
                'content': response.content,
                'last_modified': response.headers.get('Last-Modified')
            }
        
        # 其他源使用原来的逻辑
        return original_fetch_logic(url)
