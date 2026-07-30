# The Mini App: built with Vite, served by nginx, which also does the path split
# between the static frontend and the authenticated API (see nginx.conf).

FROM node:22-alpine AS build
WORKDIR /build
# package.json first so a source-only change does not reinstall dependencies.
COPY webapp/package.json webapp/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY webapp/ ./
# `npm run build` runs `tsc --noEmit` first, so a type error fails the image build
# rather than shipping a bundle that breaks at runtime.
RUN npm run build

FROM nginx:1.27-alpine
COPY webapp/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html
EXPOSE 80
