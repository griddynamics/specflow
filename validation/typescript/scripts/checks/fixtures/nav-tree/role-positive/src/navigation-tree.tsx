import { ShoppingCart, BookOpen, Tag, Settings, ClipboardList, Users } from 'lucide-react';

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
      {
        // Restricted route, own restriction.
        kind: 'link',
        labelKey: 'nav.catalog_suppliers',
        to: '/catalog/suppliers',
        icon: BookOpen,
        adminOnly: true,
      },
    ],
  },
  {
    kind: 'group',
    labelKey: 'nav.administration',
    icon: Settings,
    children: [
      {
        kind: 'link',
        labelKey: 'nav.activity_log',
        to: '/activity',
        icon: ClipboardList,
      },
      {
        kind: 'link',
        labelKey: 'nav.users',
        to: '/administration/users',
        icon: Users,
        adminOnly: true,
      },
    ],
  },
];
