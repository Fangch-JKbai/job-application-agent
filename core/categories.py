import re

def category_key(value):
    value=re.sub(r'\s+','',value or '').casefold()
    if value in {'27秋招','2027秋招'}:return '27秋招'
    if '实习' in value:return '实习'
    return value
