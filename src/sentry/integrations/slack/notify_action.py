from __future__ import absolute_import
import copy

from django import forms
from django.utils.translation import ugettext_lazy as _

from sentry import http
from sentry.rules.actions.base import EventAction
from sentry.utils import metrics, json
from sentry.models import Integration

from .utils import build_attachment

MEMBER_PREFIX = '@'
CHANNEL_PREFIX = '#'
strip_channel_chars = ''.join([MEMBER_PREFIX, CHANNEL_PREFIX])
_MAX_ITERATIONS = 100


class SlackNotifyServiceForm(forms.Form):
    workspace = forms.ChoiceField(choices=(), widget=forms.Select(
    ))
    channel = forms.CharField(widget=forms.TextInput())
    channel_id = forms.HiddenInput()
    tags = forms.CharField(required=False, widget=forms.TextInput())

    def __init__(self, *args, **kwargs):
        # NOTE: Workspace maps directly to the integration ID
        workspace_list = [(i.id, i.name) for i in kwargs.pop('integrations')]
        self.channel_transformer = kwargs.pop('channel_transformer')

        super(SlackNotifyServiceForm, self).__init__(*args, **kwargs)

        if workspace_list:
            self.fields['workspace'].initial = workspace_list[0][0]

        self.fields['workspace'].choices = workspace_list
        self.fields['workspace'].widget.choices = self.fields['workspace'].choices

    def clean(self):
        cleaned_data = super(SlackNotifyServiceForm, self).clean()

        workspace = cleaned_data.get('workspace')
        channel = cleaned_data.get('channel', '').lstrip(strip_channel_chars)

        channel_id = self.channel_transformer(workspace, channel)

        if channel_id is None and workspace is not None:
            params = {
                'channel': channel,
                'workspace': dict(self.fields['workspace'].choices).get(int(workspace)),
            }

            raise forms.ValidationError(
                _('The slack resource "%(channel)s" does not exist or has not been granted access in the %(workspace)s Slack workspace.'),
                code='invalid',
                params=params,
            )

        channel_prefix, channel_id = channel_id
        cleaned_data['channel'] = channel_prefix + channel
        cleaned_data['channel_id'] = channel_id

        return cleaned_data


class SlackNotifyServiceAction(EventAction):
    form_cls = SlackNotifyServiceForm
    label = u'Send a notification to the {workspace} Slack workspace to {channel} and show tags {tags} in notification'

    def __init__(self, *args, **kwargs):
        super(SlackNotifyServiceAction, self).__init__(*args, **kwargs)
        self.form_fields = {
            'workspace': {
                'type': 'choice',
                'choices': [(i.id, i.name) for i in self.get_integrations()]
            },
            'channel': {
                'type': 'string',
                'placeholder': 'i.e #critical'
            },
            'tags': {
                'type': 'string',
                'placeholder': 'i.e environment,user,my_tag'
            }
        }

    def is_enabled(self):
        return self.get_integrations().exists()

    def after(self, event, state):
        if event.group.is_ignored():
            return

        integration_id = self.get_option('workspace')
        channel = self.get_option('channel_id')
        tags = set(self.get_tags_list())

        try:
            integration = Integration.objects.get(
                provider='slack',
                organizations=self.project.organization,
                id=integration_id
            )
        except Integration.DoesNotExist:
            # Integration removed, rule still active.
            return

        def send_notification(event, futures):
            rules = [f.rule for f in futures]
            attachment = build_attachment(event.group, event=event, tags=tags, rules=rules)

            payload = {
                'token': integration.metadata['access_token'],
                'channel': channel,
                'attachments': json.dumps([attachment]),
            }

            session = http.build_session()
            resp = session.post('https://slack.com/api/chat.postMessage', data=payload)
            resp.raise_for_status()
            resp = resp.json()
            if not resp.get('ok'):
                self.logger.info('rule.fail.slack_post', extra={'error': resp.get('error')})

        key = u'slack:{}:{}'.format(integration_id, channel)

        metrics.incr('notifications.sent', instance='slack.notification', skip_internal=False)
        yield self.future(send_notification, key=key)

    def render_label(self):
        try:
            integration_name = Integration.objects.get(
                provider='slack',
                organizations=self.project.organization,
                id=self.get_option('workspace')
            ).name
        except Integration.DoesNotExist:
            integration_name = '[removed]'

        tags = self.get_tags_list()

        return self.label.format(
            workspace=integration_name,
            channel=self.get_option('channel'),
            tags=u'[{}]'.format(', '.join(tags)),
        )

    def get_tags_list(self):
        return [s.strip() for s in self.get_option('tags', '').split(',')]

    def get_integrations(self):
        return Integration.objects.filter(
            provider='slack',
            organizations=self.project.organization,
        )

    def get_form_instance(self):
        return self.form_cls(
            self.data,
            integrations=self.get_integrations(),
            channel_transformer=self.get_channel_id,
        )

    def get_channel_id(self, integration_id, name):
        try:
            integration = Integration.objects.get(
                provider='slack',
                organizations=self.project.organization,
                id=integration_id,
            )
        except Integration.DoesNotExist:
            return None

        session = http.build_session()

        token_payload = {
            'token': integration.metadata['access_token'],
        }

        # Look for channel ID
        channels_payload = dict(token_payload, **{
            'exclude_archived': False,
            'exclude_members': True,
        })

        channel_id, did_error = _find_matching_channel(self.logger, session, name, channels_payload)
        if did_error:
            return None

        if channel_id:
            return (CHANNEL_PREFIX, channel_id)

        # Channel may be private
        group_id, did_error = _find_matching_group(self.logger, session, name, channels_payload)
        if did_error:
            return None

        if group_id:
            return (CHANNEL_PREFIX, group_id)

        # Channel may actually be a user
        member_id, did_error = _find_matching_user(self.logger, session, name, channels_payload)
        if did_error:
            return None

        if member_id:
            return (MEMBER_PREFIX, member_id)

        return None


def _find_matching_conversation(logger, logger_message, session, url, resource_type, name, original_params):
    # See:
    # - https://api.slack.com/methods/channels.list
    # - https://api.slack.com/methods/groups.list
    # - https://api.slack.com/methods/users.list
    # WARNING: This function does not do any handling for rate limiting.
    did_error = False

    next_params = copy.deepcopy(original_params)
    # NOTE: Prefer a bounded `for` loop over a `while` loop.
    for _ in range(_MAX_ITERATIONS):
        if next_params is None:
            break

        resp = session.get(url, params=next_params)
        resp = resp.json()
        if not resp.get('ok'):
            logger.info(logger_message, extra={'error': resp.get('error')})
            did_error = True
            return None, did_error

        for c in resp[resource_type]:
            if c['name'] == name:
                return c['id'], did_error

        next_cursor = resp.get('response_metadata', {}).get('next_cursor')
        if next_cursor is None:
            next_params = None
        else:
            # NOTE: This intentionally modifies `next_params` in place.
            next_params['cursor'] = next_cursor

    return None, did_error


def _find_matching_channel(logger, session, name, original_params):
    return _find_matching_conversation(
        logger,
        'rule.slack.channel_list_failed',
        session,
        'https://slack.com/api/channels.list',
        'channels',
        name,
        original_params,
    )


def _find_matching_group(logger, session, name, original_params):
    return _find_matching_conversation(
        logger,
        'rule.slack.group_list_failed',
        session,
        'https://slack.com/api/groups.list',
        'groups',
        name,
        original_params,
    )


def _find_matching_user(logger, session, name, original_params):
    return _find_matching_conversation(
        logger,
        'rule.slack.user_list_failed',
        session,
        'https://slack.com/api/users.list',
        'members',
        name,
        original_params,
    )
