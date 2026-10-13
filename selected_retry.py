"""Parse saved detector fields for an explicitly selected retry stage."""
import re
from automation_adaptation import build_aggregation_assessment, validate_backend_fields

STAGES = {'页面预检与下载': 'precheck', '重新检测并适配': 'detect',
          '从参数提交继续': 'submit', '仅重新回放': 'replay'}


def fields_from_notes(notes):
    fields = {'ok': True, 'SDK列表': []}
    names = {'最终判断', '初始Activity', '应用类型', '激励视频聚合id', '插屏聚合id',
             '归因平台', 'af_key', 'TradPlus APP ID', 'TradPlus SDK版本'}
    # Read only the most recent generated field block, never combine runs.
    blocks = re.split(r'(?m)^最终判断\s*[:：]', str(notes or ''))
    if len(blocks) == 1:
        return fields
    block = '最终判断:' + blocks[-1]
    for line in block.splitlines():
        label, separator, value = line.replace('：', ':').partition(':')
        if not separator:
            continue
        label, value = label.strip(), value.strip()
        if label in names:
            fields[label] = value
        elif label.endswith(' SDK Key'):
            fields['SDK列表'].append({'名称': label[:-8], 'key': value})
    return fields


def validated_retry_fields(task):
    fields = fields_from_notes(task.notes)
    errors = validate_backend_fields(fields, task.package_name)
    assessment = build_aggregation_assessment(fields)
    if errors or not assessment['auto_submit'] or assessment['confidence'] != '高':
        raise ValueError(task.package_name + '：保存的参数不完整或置信度不足，请选择重新检测；' + '；'.join(errors))
    return fields
