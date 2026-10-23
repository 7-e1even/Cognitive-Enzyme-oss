# Cognitive Enzyme dashboard (Next.js :13000)
FROM node:20-alpine AS builder

WORKDIR /app

# The rewrite proxy target is baked in at standalone build time; compose points it at the backend service.
ARG CE_BACKEND_URL=http://localhost:8420
ENV CE_BACKEND_URL=$CE_BACKEND_URL
ENV DOCKER_BUILD=1

COPY package.json package-lock.json ./
RUN npm ci --legacy-peer-deps
COPY . .
RUN npm run build

FROM node:20-alpine

WORKDIR /app
ENV NODE_ENV=production
ENV HOSTNAME=0.0.0.0
ENV PORT=13000

COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public

EXPOSE 13000
CMD ["node", "server.js"]
