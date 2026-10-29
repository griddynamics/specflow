import { ShoppingCart, BookOpen, Tag } from 'lucide-react';

const COLUMNS = [
  { id: 'name', labelKey: 'catalog.column.name' },
  { id: 'price', labelKey: 'catalog.column.price' },
];

const NAV_ITEMS = [
  {
    kind: 'link',
    labelKey: 'nav.orders',
    to: '/orders',
    icon: ShoppingCart,
  },
  {
    kind: 'group',
    labelKey: 'nav.catalog',
    icon: BookOpen,
    children: [
      {
        kind: 'link',
        labelKey: 'nav.catalog_products',
        to: '/catalog/products',
        icon: Tag,
      },
    ],
  },
];
