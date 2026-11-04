export type ActionFeedbackState = {
  notice: string | null;
  error: string | null;
};

export const initialActionFeedbackState: ActionFeedbackState = {
  notice: null,
  error: null,
};

export function actionFeedbackFailure(
  error: unknown,
  fallback: string,
): ActionFeedbackState {
  return {
    notice: null,
    error: error instanceof Error && error.message ? error.message : fallback,
  };
}