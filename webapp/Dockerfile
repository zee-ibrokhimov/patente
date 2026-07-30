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

# The 409 sign figures, served as static files rather than through the API.
#
# They cannot go behind the initData header: an <img src> tag sends no custom
# headers, so the browser's request arrives unsigned and is refused. Serving them
# here instead is both the fix and the better design — they are public-domain
# ministerial figures with no user data in them, nginx caches them properly for a
# bank that shows the same signs repeatedly, and it keeps 3.7 MB of image traffic
# off a single-process API that must stay free to answer questions.
COPY content/out/images /usr/share/nginx/html/figures
EXPOSE 80
