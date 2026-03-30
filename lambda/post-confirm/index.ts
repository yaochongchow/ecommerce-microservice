import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, PutCommand } from '@aws-sdk/lib-dynamodb';

const dynamo = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const USERS_TABLE = process.env.USERS_TABLE!;

export const handler = async (event: any) => {
  const { sub, email, given_name, family_name } = event.request.userAttributes;

  await dynamo.send(new PutCommand({
    TableName: USERS_TABLE,
    ConditionExpression: 'attribute_not_exists(userId)', // don't overwrite existing
    Item: {
      userId:    sub,
      email,
      firstName: given_name  ?? '',
      lastName:  family_name ?? '',
      createdAt: Math.floor(Date.now() / 1000),
      updatedAt: Math.floor(Date.now() / 1000),
    },
  }));

  // Must return the event back to Cognito
  return event;
};