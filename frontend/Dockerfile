# Build stage
FROM node:22-alpine AS build
WORKDIR /app
ARG VITE_BACKEND_URL
ENV VITE_BACKEND_URL=$VITE_BACKEND_URL
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

# Production stage — serve static files with Nginx
FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.d/10-backend-upstream.sh /docker-entrypoint.d/10-backend-upstream.sh
RUN chmod +x /docker-entrypoint.d/10-backend-upstream.sh
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
