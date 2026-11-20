import { createModels } from '@earendil-works/pi-ai/models';
import { fauxAssistantMessage, fauxProvider, fauxToolCall } from '@earendil-works/pi-ai/providers/faux';

export const issue = { number: 42, url: 'https://github.com/ipdelete/floe/issues/42' };
export const draftCall = fauxToolCall('create_feature_issue', { title: 'Skip tasks', description: 'Skip some tasks.' });
export const searchCall = fauxToolCall('search_features', { query: 'skip tasks' });
export const toolTurn = (calls) => fauxAssistantMessage(calls, { stopReason: 'toolUse' });
export const createDraft = () => toolTurn(draftCall);

export function fakeModel(respond = createDraft, first = toolTurn(searchCall)) {
  const faux = fauxProvider();
  faux.setResponses([first, respond, fauxAssistantMessage('Done')]);
  const models = createModels();
  models.setProvider(faux.provider);
  return { models, model: { provider: 'faux', modelId: 'faux-1' } };
}
