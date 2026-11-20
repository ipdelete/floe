import { createModels } from '@earendil-works/pi-ai/models';
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';

export const issue = { number: 42, url: 'https://github.com/ipdelete/floe/issues/42' };
export const createDraft = () => fauxAssistantMessage(
  fauxToolCall('create_feature_issue', { title: 'Skip tasks', description: 'Skip some tasks.' }),
  { stopReason: 'toolUse' },
);

export function fakeModel(respond = createDraft) {
  const faux = fauxProvider();
  faux.setResponses([respond, fauxAssistantMessage('Done')]);
  const models = createModels();
  models.setProvider(faux.provider);
  return { models, model: { provider: 'faux', modelId: 'faux-1' } };
}
