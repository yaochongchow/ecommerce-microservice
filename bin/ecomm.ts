#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { M1PlatformStack } from "../lib/m1-platform-stack";


const app = new cdk.App();

const env: cdk.Environment = {
  account: app.node.tryGetContext("account") ?? process.env.CDK_DEFAULT_ACCOUNT,
  region:  app.node.tryGetContext("region")  ?? process.env.CDK_DEFAULT_REGION ?? "us-east-1",
};

const platform = new M1PlatformStack(app, "EcommPlatformStack", {
  env,
  description: "M1 — Platform + gateway + User service",
});