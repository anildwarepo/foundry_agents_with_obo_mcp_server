#!/bin/bash

# Usage: ./docker_build.sh -a <acr_name> -i <image_name> -v <version> [-b]
# -b flag enables ACR build instead of local Docker build

USE_ACR_BUILD=false

while getopts "a:i:v:b" opt; do
    case $opt in
        a) ACR_NAME="$OPTARG" ;;
        i) IMAGE_NAME="$OPTARG" ;;
        v) IMAGE_VERSION="$OPTARG" ;;
        b) USE_ACR_BUILD=true ;;
        \?) echo "Invalid option -$OPTARG" >&2; exit 1 ;;
    esac
done

if [ -z "$ACR_NAME" ] || [ -z "$IMAGE_NAME" ] || [ -z "$IMAGE_VERSION" ]; then
    echo "Usage: ./docker_build.sh -a <acr_name> -i <image_name> -v <version> [-b]"
    echo "  -a  ACR name (required)"
    echo "  -i  Image name (required)"
    echo "  -v  Image version (required)"
    echo "  -b  Use ACR build instead of local Docker build (optional)"
    exit 1
fi

az acr login --name $ACR_NAME

if [ "$USE_ACR_BUILD" = true ]; then
    # Build directly in ACR (no local Docker required)
    az acr build --registry $ACR_NAME --image ${IMAGE_NAME}:$IMAGE_VERSION .
else
    # Local Docker build, tag, and push
    docker build -t ${IMAGE_NAME}:$IMAGE_VERSION .
    docker tag ${IMAGE_NAME}:$IMAGE_VERSION ${ACR_NAME}.azurecr.io/${IMAGE_NAME}:$IMAGE_VERSION
    docker push ${ACR_NAME}.azurecr.io/${IMAGE_NAME}:$IMAGE_VERSION
fi