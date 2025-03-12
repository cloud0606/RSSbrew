import re
from bs4 import BeautifulSoup
import logging
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
import os
from openai import OpenAI
import tiktoken
import time

logger = logging.getLogger('feed_logger')
OPENAI_PROXY = os.environ.get('OPENAI_PROXY')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
OPENAI_BASE_URL = os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1'

def remove_control_characters(s):
    control_chars = ''.join(map(chr, range(0, 32))) + chr(127)
    control_char_re = re.compile('[%s]' % re.escape(control_chars))
    return control_char_re.sub('', s)

def clean_url(url):
    parsed_url = urlparse(url)
    
    # 解析查询字符串
    query_params = parse_qs(parsed_url.query)
    
    # 移除 'hl' 参数（如果存在）
    query_params.pop('hl', None)
    
    # 重新构建查询字符串
    new_query = urlencode(query_params, doseq=True)
    
    # 重建 URL，包括新的查询字符串，不包括片段
    clean_url = urlunparse((
        parsed_url.scheme,
        parsed_url.netloc,
        parsed_url.path.rstrip('/'),
        '',
        new_query,
        ''
    ))
    
    # 转换为小写
    clean_url = clean_url.lower()
    
    # 如果清理后的 URL 以 '?' 结尾，则移除它
    if clean_url.endswith('?'):
        clean_url = clean_url[:-1]
    
    return clean_url

def clean_html(html_content):
    """
    This function is used to clean the HTML content.
    It will remove all the <script>, <style>, <img>, <a>, <video>, <audio>, <iframe>, <input> tags.
    Returns:
        Cleaned text for summarization
    """
    soup = BeautifulSoup(html_content, "html.parser")

    for script in soup.find_all("script"):
        script.decompose()

    for style in soup.find_all("style"):
        style.decompose()

    for img in soup.find_all("img"):
        img.decompose()

    for a in soup.find_all("a"):
        a.decompose()

    for video in soup.find_all("video"):
        video.decompose()

    for audio in soup.find_all("audio"):
        audio.decompose()
    
    for iframe in soup.find_all("iframe"):
        iframe.decompose()
    
    for input in soup.find_all("input"):
        input.decompose()

    return soup.get_text()

def clean_txt_and_truncate(query, model, clean_bool=True):
    cleaned_article = query
    if clean_bool:
        cleaned_article = clean_html(query)
    try:
        encoding = tiktoken.encoding_for_model(model)
    except:
        encoding = tiktoken.encoding_for_model('gpt-4o')
    token_length = len(encoding.encode(cleaned_article))

    max_length_of_models = {
        'gpt-3.5-turbo': 16200,
        'gpt-4o': 127800,
        'gpt-4-turbo': 127800,
        'gpt-4o-mini': 127800,
        'default': 127800  # Default for all other models
    }

    max_length = max_length_of_models.get(model, max_length_of_models['default'])

    # Truncate the text if it exceeds the model's token limit
    if token_length > max_length:
        truncated_article = encoding.decode(encoding.encode(cleaned_article)[:max_length])
        return truncated_article
    else:
        return cleaned_article

def generate_untitled(entry):
    try: return entry.title
    except: 
        try: return entry.article[:50]
        except: return entry.link

def passes_filters(entry, processed_feed, filter_type):
    groups = processed_feed.filter_groups.filter(usage=filter_type)
    if not groups:
        return True
    group_results = []
    for group in groups:
        filters = group.filters.all()
        results = [match_content(entry, filter) for filter in filters]
        logger.debug(f'  Results for group {group.usage}: {results} for {entry.title} {entry.link}')
        if group.relational_operator == 'all':
            group_results.append(all(results))
        elif group.relational_operator == 'any':
            group_results.append(any(results))
        elif group.relational_operator == 'none':
            group_results.append(not any(results))
    if filter_type == 'feed_filter':
        group_relational_operator = processed_feed.feed_group_relational_operator
    elif filter_type == 'summary_filter':
        group_relational_operator = processed_feed.summary_group_relational_operator

    logger.debug(f'  Group results for {filter_type}: {group_results} for {entry.title}')
    if group_relational_operator == 'all':
        return all(group_results)
    elif group_relational_operator == 'any':
        return any(group_results)
    elif group_relational_operator == 'none':
        return not any(group_results)

def match_content(entry, filter):
    content = ''
    if filter.field in ['title', 'title_or_content']:
        content += generate_untitled(entry) + ' '
    if filter.field in ['content', 'title_or_content']:
        try:
            content += entry.content[0].value + ' '
        except:
            pass
        try:
            content += entry.description + ' '
        except:
            pass
    elif filter.field == 'link':
        content = entry.link
    if not content.strip(): # Strip is necessary for removing leading and trailing spaces
        return False

    if filter.match_type == 'contains':
        return filter.value in content
    elif filter.match_type == 'does_not_contain':
        return filter.value not in content
    elif filter.match_type == 'matches_regex':
        return re.search(filter.value, content) is not None
    elif filter.match_type == 'does_not_match_regex':
        return re.search(filter.value, content) is None
    elif filter.match_type == 'shorter_than':
        return len(content) < int(filter.value)
    elif filter.match_type == 'longer_than':
        return len(content) > int(filter.value)


def remove_think_part(response):
    start_tag = "<think>"
    end_tag = "</think>"
    while start_tag in response and end_tag in response:
        start_index = response.find(start_tag)
        end_index = response.find(end_tag) + len(end_tag)
        if start_index != -1 and end_index != -1:
            response = response[:start_index] + response[end_index:]
    return response


def generate_summary(article, model, output_mode='json', prompt=None, other_model=''):
    if model == 'other':
        model = other_model
    if not model or not OPENAI_API_KEY:
        logger.warning('  OpenAI API key or model not set, skipping summary generation')
        return 
    try:
        client_params = {
            "api_key": OPENAI_API_KEY,
            "base_url": OPENAI_BASE_URL
        }

        completion_params = {
            "model": model,
        }

        # start_time = time.time()

        # # DeepSeek 的处理逻辑
        # logger.debug(f"                    [-] 使用deepseek-r1模型生成摘要")
        # client = OpenAI(**client_params)
        # truncated_query = clean_txt_and_truncate(article.content, model, clean_bool=True)
        # messages_tt=[{
        #     "role":"user",
        #     "content": prompt + '  原文内容是 : ' + truncated_query
        #     }]
        # logger.debug(f"                    [-] prompt is 【{prompt[:100] + '  原文内容是 : ' + truncated_query[:100]}】 \n.... \n")
        # completion = client.chat.completions.create(
        #     model="deepseek-ai/deepseek-r1",
        #     messages=messages_tt,
        #     temperature=0.6,
        #     top_p=0.7,
        #     max_tokens=4096,
        #     stream=True
        #     )

        # for chunk in completion:
        #     if chunk.choices[0].delta.content is not None:
        #         print(chunk.choices[0].delta.content, end="")
            
        # end_time = time.time()
        # elapsed_time = end_time - start_time
        # logger.info(f"                    [-] Deepseek摘要生成耗时: {elapsed_time:.2f}秒")
        # return remove_think_part(completion.choices[0].message.content)


        # OPENAI的处理逻辑
        if OPENAI_PROXY:
            client_params["http_client"] = httpx.Client(proxy=OPENAI_PROXY)
        client = OpenAI(**client_params)
        # 自定义prompt 
        #prompt = ''' '''
        if output_mode == 'json':
            truncated_query = clean_txt_and_truncate(article.content, model, clean_bool=True)
            #additional_prompt = f"Please summarize this article, and output the result only in JSON format. First item of the json is a one-line summary in 15 words named as 'summary_one_line', second item is the 150-word summary named as 'summary_long'. Output result in {language} language."
            messages = [
                {
                    "role": "system", 
                    "content": '''
                            你是一个用于总结文章的有用助手，输出采用JSON格式，一定要输出纯文本的json格式，使用{做开头，}做结尾，不要有任何其他的用于标注代码块的符号。 eg:
                            {
                            "summary_one_line": "新型多态攻击伪装密码管理器作案",
                            "summary_long": "3月6日，攻击者采用新型多态攻击方式，使用Chrome扩展滥用chrome.management API，冒充合法密码管理器（如1Password），伪造登录页面诱骗用户输入敏感信息。完成钓鱼操作后，恶意扩展恢复原状，致使攻击难以被察觉。研究人员建议谷歌尽快加强防护措施，目前尚无有效对策。",
                            "title": "恶意Chrome扩展伪装密码管理器实施多态攻击窃取敏感信息",
                            "tag": "网络攻击"
                            }
                            '''
                },
                {"role": "user", "content": f"<prompt> {prompt}</prompt> \n  <article> {truncated_query}</article> "},
                # {"role": "assistant", "content": f"<prompt> {prompt}</prompt>"},
            ]
            completion_params["response_format"] = { "type": "json_object" }
            completion_params["messages"] = messages
        elif output_mode == 'HTML':
            truncated_query = clean_txt_and_truncate(article.content, model, clean_bool=False)
            messages = [
                {"role": "system", "content": "You are a helpful assistant for summarizing article content, designed to output pure and clean json format, do not code block the output using triple backticks."},
                {"role": "user", "content": f"<article> {truncated_query}</article> \n <prompt> {prompt}</prompt>"},
                # {"role": "assistant", "content": f"<prompt> {prompt}</prompt>"},
            ]
            completion_params["messages"] = messages
        elif output_mode == 'md':
            # 自定义摘要生成过程
            truncated_query = clean_txt_and_truncate(article.content, model, clean_bool=False)
            messages = [
                {"role": "system", "content": "You are a helpful assistant for summarizing article content, designed to output pure and clean json format, do not code block the output using triple backticks."},
                {"role": "user", "content": f"<article> {truncated_query}</article> \n <prompt> {prompt}</prompt>"},
                # {"role": "assistant", "content": f"<prompt> {prompt}</prompt>"},
            ]
            completion_params["messages"] = messages

        completion = client.chat.completions.create(**completion_params)
        logger.debug(f"prompt is 【{prompt[:50]}】 \n.... \n")
        logger.debug(f"content is 【{truncated_query}】 \n.... \n")
        return completion.choices[0].message.content
        # OPENAI的处理逻辑 end
        

    except Exception as e:
        logger.error(f'Failed to generate summary for article {article.title}: {str(e)}')
    
def parse_cron(cron_string):
    parts = cron_string.split()
    if len(parts) != 5:
        raise ValueError("CRON string must have exactly 5 parts separated by spaces (minute, hour, day of month, month, day of week)")
    return {
        'minute': parts[0],
        'hour': parts[1],
        'day': parts[2],
        'month': parts[3],
        'day_of_week': parts[4]
    }
