"""
Copyright 2018 Amazon.com, Inc. or its affiliates.
All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License").
You may not use this file except in compliance with the License.
A copy of the License is located at
   http://aws.amazon.com/apache2.0/
or in the "license" file accompanying this file.

This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.

This script orchestrates the enablement and centralization of GuardDuty
across an enterprise of AWS accounts. It takes in a list of AWS Account
Numbers, iterates through each account and region to enable GuardDuty.
It creates each account as a Member in the GuardDuty Master account.
It invites and accepts the invite for each Member account.
"""

import json
import boto3
import inspect
import logging
import os
import urllib3
import botocore
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.getLogger('boto3').setLevel(logging.CRITICAL)
logging.getLogger('botocore').setLevel(logging.CRITICAL)

gdmaster_account_number = os.environ['gd_master_account']
role_to_assume = os.environ['role_to_assume']
session = boto3.Session()
failed_regions = []
failed_members = {}
http = urllib3.PoolManager()


def handler(event, context):
    logger.debug(f'boto3 version: {boto3.__version__}')
    logger.debug(f'botocore version: {botocore.__version__}')

    guardduty_regions = session.get_available_regions('guardduty')
    gdmaster_account_session = assume_role(
        gdmaster_account_number,
        role_to_assume
    )
    bucket_arn = create_s3_destination(gdmaster_account_session)

    for region in guardduty_regions:
        try:
            enable_gd_master(region)
            enable_gd_member(gdmaster_account_session, region, bucket_arn)
        except Exception as e:
            logger.error(
                f'Error to enable master or member in region {region}',
                exc_info=True
            )
            continue

    responseValue = 120
    responseData = {}
    responseData['Data'] = responseValue
    cfnresponse(event, context, 'SUCCESS', responseData)

    # log unprocessed account only when they are not empty
    if failed_regions:
        logger.info('Failed to enable GuardDuty master: ')
        logger.info(json.dumps(failed_regions, indent=2))
    if bool(failed_members):
        logger.info('Failed to enable GuardDuty members: ')
        logger.info(json.dumps(failed_members, indent=2))


def cfnresponse(
        event,
        context,
        responseStatus,
        responseData={},
        physicalResourceId=None,
        noEcho=False
):
    ls = context.log_stream_name
    responseBody = {}
    responseBody['Status'] = responseStatus
    responseBody['Reason'] = 'See the details in CloudWatch Log Stream: ' + ls
    responseBody['PhysicalResourceId'] = physicalResourceId or ls
    responseBody['StackId'] = event['StackId']
    responseBody['RequestId'] = event['RequestId']
    responseBody['LogicalResourceId'] = event['LogicalResourceId']
    responseBody['NoEcho'] = noEcho
    responseBody['Data'] = responseData
    json_responseBody = json.dumps(responseBody)
    headers = {
        'content-type': '',
        'content-length': str(len(json_responseBody))
    }

    try:
        response = http.request(
            'PUT',
            event['ResponseURL'],
            body=json_responseBody.encode('utf-8'),
            headers=headers
        )
        logger.debug('Status code: ' + response.reason)
    except Exception as e:
        logger.error(
          'cfnresponse(..) failed executing requests.put(..): ' + str(e)
        )


def assume_role(aws_account_number, role_name):
    """
    Assumes the provided role in each account and returns a GuardDuty client
    :param aws_account_number: AWS Account Number
    :param role_name: Role to assume in target account
    :param aws_region:
        AWS Region for the Client call, not required for IAM calls
    :return: GuardDuty client in the specified AWS Account and Region
    """

    # Beginning the assume role process for account
    sts_client = boto3.client('sts')
    partition = sts_client.get_caller_identity()['Arn'].split(':')[1]

    response = sts_client.assume_role(
        RoleArn=f'arn:{partition}:iam::{aws_account_number}:role/{role_name}',
        RoleSessionName='EnableGuardDuty'
    )

    # Storing STS credentials
    sts_session = boto3.Session(
        aws_access_key_id=response['Credentials']['AccessKeyId'],
        aws_secret_access_key=response['Credentials']['SecretAccessKey'],
        aws_session_token=response['Credentials']['SessionToken']
    )
    logger.debug(f'Assumed session for {aws_account_number}.')

    return sts_session


def enable_gd_master(region):
    """
    Enable the delegated admin account from the AWS Organization root
    account
    :param region: the region where GuardDuty master to be enabled
    """
    logger.info(f'Enabling gd master for region {region}')
    master = session.client('guardduty', region_name=region)
    try:
        master.enable_organization_admin_account(
            AdminAccountId=gdmaster_account_number
        )
    except Exception as e:
        logger.info(
            f'Skipping Account {gdmaster_account_number} '
            f'in region {region}. Already enabled'
        )
        logger.info(
            f'Error to enable GD master in region {region}',
            exc_info=True
        )


def disable_gd_master(region):
    """
    Disable the delegated admin account from the AWS Organization root
    account for testing purpose only
    :param region: the region where GuardDuty master to be enabled
    """
    logger.info(f'disabling gd master for region {region}')
    master = boto3.client('guardduty', region_name=region)
    try:
        master.disable_organization_admin_account(
            AdminAccountId=gdmaster_account_number
        )
    except Exception as e:
        failed_regions.append(region)
        logger.info(
            f'SkippingAccount {gdmaster_account_number} '
            f'in region {region}. Already disabled'
        )


def enable_gd_member(session, region, properties):
    """
    Enable the delegated admin account from the AWS Organization root
    account for testing purpose only
    :param session: STS sesion of the GuardDuty master account
    :param region: the region wherre GuardDuty master to be enabled
    :param properties: properties for the GuardDuty publishing destination
    """
    delegated_admin_client = session.client('guardduty', region_name=region)

    try:
        detector_ids = delegated_admin_client.list_detectors()
    except Exception as ex:
        logger.info(f'      Unable to list detectors in region {region}')
        return
    logger.debug(f'detector ids is {detector_ids}')

    detector_id = detector_ids['DetectorIds'][0]
    logger.debug(f'detector id is {detector_id}')
    delegated_admin_client.create_publishing_destination(
        DetectorId=detector_id,
        DestinationType='S3',
        DestinationProperties=properties,
        ClientToken=detector_id
    )
    accounts = session.client('organizations').list_accounts()
    details = []
    failed_accounts = []

    for account in accounts['Accounts']:
        if (account['Id'] != gdmaster_account_number):
            details.append(
                {
                    'AccountId': account['Id'],
                    'Email': account['Email']
                }
            )

    try:
        unprocessed_accounts = delegated_admin_client.create_members(
            DetectorId=detector_id,
            AccountDetails=details
        )['UnprocessedAccounts']
        if (len(unprocessed_accounts) > 0):
            failed_accounts.append(unprocessed_accounts)

        delegated_admin_client.update_organization_configuration(
            DetectorId=detector_id,
            AutoEnable=True
        )
    except ClientError as e:
        logger.debug(f'Error Processing Account {account}')
        failed_accounts.append({account: repr(e)})

    logger.debug(f'region is {region}')

    if bool(failed_accounts):
        failed_members.update(region=failed_accounts)


def create_kms_key(session, region):
    """
    Create the KMS key required for GuardDuty publishing destination
    in the specified region
    :param session: STS sesion of the GuardDuty master account
    :param region: the region wherre GuardDuty master to be enabled
    :return: arn of the KMS key to be crreated
    """
    kms_client = session.client('kms', region_name=region)
    key_alias = 'alias/controltower/guardduty'

    try:
        key_response = kms_client.describe_key(KeyId=key_alias)
        logger.info(f'Existing key {key_alias} found.')
        return key_response['KeyMetadata']['Arn']
    except Exception as e:
        logger.info(f'Creating new encryption {key_alias}key')
        key_policy = {
            'Version': '2012-10-17',
            'Id': 'auto-controltower-guardduty',
            'Statement': [
                {
                    'Sid': 'Enable IAM User Permissions',
                    'Effect': 'Allow',
                    'Principal': {
                        'AWS': f'arn:aws:iam::{gdmaster_account_number}:root'
                    },
                    'Action': 'kms:*',
                    'Resource': '*'
                },
                {
                    'Sid': 'Allow access for Key Administrators',
                    'Effect': 'Allow',
                    'Principal': {
                        'AWS': f'arn:aws:iam::{gdmaster_account_number}'
                        ':role/AWSControlTowerExecution'
                    },
                    'Action': [
                        'kms:Create*',
                        'kms:Describe*',
                        'kms:Enable*',
                        'kms:List*',
                        'kms:Put*',
                        'kms:Update*',
                        'kms:Revoke*',
                        'kms:Disable*',
                        'kms:Get*',
                        'kms:Delete*',
                        'kms:TagResource',
                        'kms:UntagResource',
                        'kms:ScheduleKeyDeletion',
                        'kms:CancelKeyDeletion'
                    ],
                    'Resource': '*'
                },
                {
                    'Sid': 'Allow use of the key',
                    'Effect': 'Allow',
                    'Principal': {
                        'Service': 'guardduty.amazonaws.com'
                    },
                    'Action': [
                        'kms:Encrypt',
                        'kms:Decrypt',
                        'kms:ReEncrypt*',
                        'kms:GenerateDataKey*',
                        'kms:DescribeKey'
                    ],
                    'Resource': '*'
                },
                {
                    'Sid': 'Allow attachment of persistent resources',
                    'Effect': 'Allow',
                    'Principal': {
                        'Service': 'guardduty.amazonaws.com'
                    },
                    'Action': [
                        'kms:CreateGrant',
                        'kms:ListGrants',
                        'kms:RevokeGrant'
                    ],
                    'Resource': '*',
                    'Condition': {
                        'Bool': {
                            'kms:GrantIsForAWSResource': 'true'
                        }
                    }
                }
            ]
        }

        key_policy = json.dumps(key_policy)
        key_result = kms_client.create_key(
            Policy=key_policy,
            Description='The key to encrypt/decrypt GuardDuty findings'
        )
        kms_client.create_alias(
            AliasName=key_alias,
            TargetKeyId=key_result['KeyMetadata']['KeyId']
        )
        return key_result['KeyMetadata']['Arn']


def create_s3_destination(sts_session):
    """
    Create the s3 publishing destination for GuardDuty in the Control Tower
    central logging account.  GuardDuty findings are encrypted using
    the KMS key in the GuardDuty master account.
    :param sts_session: STS sesion of the GuardDuty master account
    :return: properties for the GuardDuty publishing destination
    """
    cloud_trail_client = session.client('cloudtrail')
    cloud_trail_response = cloud_trail_client.describe_trails(
        trailNameList=[
            'aws-controltower-BaselineCloudTrail',
        ]
    )

    logger.debug(cloud_trail_response)

    trail_bucket_name = cloud_trail_response['trailList'][0]['S3BucketName']
    bucket_name = trail_bucket_name.replace('logs', 'guardduty')
    bucket_prefix = cloud_trail_response['trailList'][0]['S3KeyPrefix']
    bucket_region = cloud_trail_response['trailList'][0]['HomeRegion']
    bucket_account = trail_bucket_name.split('-')[3]

    logger.debug(f'trail_bucket_name is {trail_bucket_name}')
    logger.debug(f'bucket_name is {bucket_name}')
    logger.debug(f'bucket_prefix is {bucket_prefix}')
    logger.debug(f'bucket_region is {bucket_region}')
    logger.debug(f'bucket_account is {bucket_account}')

    log_account_session = assume_role(bucket_account, role_to_assume)
    # Create s3 bucket in centralized log account of Control Tower
    s3_client = log_account_session.client('s3')

    # Only these regions are allowed in LocationConstraint
    # see documentation under S3.Client.create_bucket API
    allowed_regions = [
        'eu-west-1',
        'us-west-1',
        'us-west-2',
        'ap-south-1',
        'ap-southeast-1',
        'ap-southeast-2',
        'ap-northeast-1',
        'sa-east-1',
        'cn-north-1',
        'eu-central-1'
        ]

    # KMS key must be in the same region as the s3 bucket,
    # so we create bucket and key together.
    kms_key_arn = ''
    try:
        if bucket_region in allowed_regions:
            s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={
                    'LocationConstraint': bucket_region
                }
            )
            kms_key_arn = create_kms_key(sts_session, bucket_region)
        elif bucket_region.startswith('eu'):
            s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={
                    'LocationConstraint': 'EU'
                }
            )
            kms_key_arn = create_kms_key(sts_session, bucket_region)
        else:
            # Bucket will be created in us-east-1
            s3_client.create_bucket(Bucket=bucket_name)
            kms_key_arn = create_kms_key(sts_session, 'us-east-1')
    except Exception as e:
        logger.info(f'Bucket {bucket_name} already created.')
        logger.error(f'Skipping creating bucket {bucket_name}', exc_info=True)

    bucket_policy = {
        'Version': '2012-10-17',
        'Statement': [
            {
                'Sid': 'AWSBucketPermissionsCheck',
                'Effect': 'Allow',
                'Principal': {
                    'Service': 'guardduty.amazonaws.com'
                },
                'Action': [
                    's3:GetBucketAcl',
                    's3:ListBucket',
                    's3:GetBucketLocation'
                ],
                'Resource': f'arn:aws:s3:::{bucket_name}'
            },
            {
                'Sid': 'AWSBucketDelivery',
                'Effect': 'Allow',
                'Principal': {
                    'Service': 'guardduty.amazonaws.com'
                },
                'Action': 's3:PutObject',
                'Resource': f'arn:aws:s3:::{bucket_name}/*'
            },
            {
                'Sid': 'Deny unencrypted object uploads. This is optional',
                'Effect': 'Deny',
                'Principal': {
                    'Service': 'guardduty.amazonaws.com'
                },
                'Action': 's3:PutObject',
                'Resource': f'arn:aws:s3:::{bucket_name}/*',
                'Condition': {
                    'StringNotEquals': {
                        's3:x-amz-server-side-encryption': 'aws:kms'
                    }
                }
            },
            {
                'Sid': 'Deny incorrect encryption header. This is optional',
                'Effect': 'Deny',
                'Principal': {
                    'Service': 'guardduty.amazonaws.com'
                },
                'Action': 's3:PutObject',
                'Resource': f'arn:aws:s3:::{bucket_name}/*',
                'Condition': {
                    'StringNotEquals': {
                        's3:x-amz-server-side-encryption-aws-kms-key-id': kms_key_arn
                    }
                }
            },
            {
                'Sid': 'Deny non-HTTPS access',
                'Effect': 'Deny',
                'Principal': '*',
                'Action': 's3:*',
                'Resource': f'arn:aws:s3:::{bucket_name}/*',
                'Condition': {
                    'Bool': {
                        'aws:SecureTransport': 'false'
                    }
                }
            }
        ]
    }

    # Convert the policy from JSON dict to string
    bucket_policy = json.dumps(bucket_policy)

    s3_client.put_bucket_policy(Bucket=bucket_name, Policy=bucket_policy)
    s3_client.put_bucket_encryption(
        Bucket=bucket_name,
        ServerSideEncryptionConfiguration={
            'Rules': [
                {
                    'ApplyServerSideEncryptionByDefault': {
                        'SSEAlgorithm': 'AES256'
                    }
                },
            ]
        }
    )

    destination_properties = {
            'DestinationArn': f'arn:aws:s3:::{bucket_name}',
            'KmsKeyArn': kms_key_arn
    }
    return destination_properties