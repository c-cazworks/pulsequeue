FROM node:24-alpine AS builder
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:stable-alpine AS runtime
# LOCAL_RESOLVERS exports the container's DNS server to the template. That resolver ignores
# search domains, so on Kubernetes set API_UPSTREAM to the service FQDN.
ENV NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
    API_UPSTREAM=api:8000
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=builder /src/dist /usr/share/nginx/html
EXPOSE 8080
