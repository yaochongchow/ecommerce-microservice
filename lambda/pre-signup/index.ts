export const handler = async (event: any) => {
  event.response.autoConfirmUser  = true;
  event.response.autoVerifyEmail  = true; 
  // Optional: auto-verify email to skip verification code step
  return event;
};
